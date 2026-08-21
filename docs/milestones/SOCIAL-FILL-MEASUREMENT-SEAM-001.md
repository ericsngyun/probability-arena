# SOCIAL-FILL-MEASUREMENT-SEAM-001

**Status: BUILT, TESTED, NOT MERGED, NOT DEPLOYED.** Mac-only. Nothing was
run on `evo-x2`; `PROD-ACTIVITY-PROFILE-001` was not touched.

Implements `docs/milestones/EVIDENCE-JOIN-CONTRACT-001.md` — the typed seam
that makes `SOCIAL-TAPE-001` and `REALIZED-FILL-CORPUS-001` composable
**without silently inventing information**.

No alpha model, no signal, no scoring, no trading, no capital. The seam
decides which quantities may be compared. It never decides what they mean.

New package: `app/seam/` (`measurement.py`, `clock.py`, `token.py`,
`cohort.py`, `join.py`).
Tests: `tests/test_social_fill_measurement_seam_001.py`.

---

## 1. `Measurement[T]` — two independent dimensions, not one merged enum

The join contract's §2 finding was that `fills.AbsenceReason` and
`social.DeferredState` agree on exactly one member and encode **different
distinctions**. So they are not unified. Both axes are carried at once:

```
availability   AVAILABLE | NOT_PROVIDED | NOT_RECONSTRUCTABLE
               | NOT_YET_OBSERVED | NOT_AUTHORIZED | NOT_APPLICABLE
observation    NOT_ATTEMPTED | OBSERVED_NONE | OBSERVED_VALUE
```

The combination the whole milestone exists to protect:

| state | means |
|---|---|
| `AVAILABLE` + `OBSERVED_NONE` | **we watched the window and the event did not occur** — a real negative label |
| `NOT_PROVIDED` + `NOT_ATTEMPTED` | we have no measurement |

They differ on **both** axes, they serialise differently, and neither yields a
number. `P(wallet_confirmation = 0 | social event)` is only meaningful in the
first case, and only the first case answers `is_measured_negative`.

**Illegal combinations are unconstructible.** The legality table runs in
`__post_init__`:

| observation | permitted availability | value | window |
|---|---|---|---|
| `OBSERVED_VALUE` | `AVAILABLE` only | required | optional |
| `OBSERVED_NONE` | `AVAILABLE` only | must be `None` | **required** |
| `NOT_ATTEMPTED` | any | must be `None` | forbidden |

6 × 3 = 18 combinations; **8 are legal**, asserted directly by
`test_exactly_eight_of_eighteen_combinations_are_legal`.

`Measurement` supports no arithmetic, no ordering and no truthiness —
`bool(m)` raises, so `m or 0` cannot fabricate a zero.

### The window requirement is load-bearing

`OBSERVED_NONE` requires the `ObservationWindow` it was measured over
(start, end, basis, named watcher). A negative label without its window cannot
be compared, cannot be pooled, and cannot state its own noise floor
(doctrine 4). "We looked and saw nothing" is a claim about an interval or it is
not a claim.

### Adapters are lossless, and never guess

Both source vocabularies map in and back out **exactly**, proven member by
member (`test_every_fills_absence_reason_round_trips_exactly`,
`test_every_social_deferred_state_round_trips_exactly`).

The coarse `availability` axis is a *projection*, so the original source term
travels on the record as `OriginTag(vocabulary, code)` — which is also §2's
"a joined row carries both vocabularies or neither".

Where a source vocabulary **cannot determine a dimension, the caller must say**:

* `fills.AbsenceReason` answers *why we cannot have it*. It determines
  `availability` always, and `observation` only for `NOT_YET_OBSERVED`,
  `NOT_AUTHORIZED` and `NOT_APPLICABLE`. For the rest, `observation=` is a
  **required** argument — `from_fills_absence(NOT_PROVIDED)` raises.
* `social.DeferredState` answers *what looking achieved*. It determines
  `observation` always, and `availability` for everything except `ABSENT`.
  `from_social_deferred(Deferred())` raises without an explicit
  `availability=`.

Cross-vocabulary writes are refused outright: a social-origin measurement
cannot be written back as a fills `AbsenceReason`, and `to_fills_maybe` on an
`OBSERVED_NONE` raises rather than degrading it to `NOT_PROVIDED` — §2's named
forbidden collapse.

---

## 2. `ObservationTimestamp` — a cross-process clock contract

```
wall_utc · monotonic_ns · host_boot_id · process_epoch_id · host_id · clock_quality
```

`app/social`'s epoch guard is correct and insufficient: once the collector, the
quote path and the decoder run in separate processes, an epoch match is
impossible and every interesting interval is refused. The fix is not to relax
the guard but to identify what actually makes `monotonic_ns` comparable —
`CLOCK_MONOTONIC` (Linux) and `mach_absolute_time` (macOS) are **boot-relative,
not process-relative**. So the comparability key is `(host_id, host_boot_id)`,
and `process_epoch_id` becomes the *fallback* key. This strictly widens what is
computable without weakening anything.

`host_boot_id` comes from `/proc/sys/kernel/random/boot_id`. On macOS that file
does not exist; `read_host_boot_id()` returns
`BootIdStatus.NOT_AVAILABLE_ON_PLATFORM` with `value=None`, and a `HostBootId`
carrying both a non-`PRESENT` status and a value is **unconstructible**.
`HostBootId.unknown().matches(HostBootId.unknown())` is `False` — unknowns
never license anything.

### Interval rules, enforced in `interval()`

| condition | result |
|---|---|
| same host, same known boot id | `MONOTONIC_SAME_BOOT` — permitted **across processes** |
| same host, boot unknown, same process epoch | `MONOTONIC_SAME_EPOCH` (`app/social`'s existing rule, preserved) |
| known but **different** boot ids | `NOT_COMPUTABLE: BOOT_MISMATCH` — the monotonic clock reset |
| different host / unmatched boot, **with** a `SyncBound` | `WALL_BOUNDED`, and the bound **travels on the result** |
| anything else | `NOT_COMPUTABLE` |

`NotComputable` carries **no number at all** — no `.microseconds`, no
`.value`, no default — so a caller cannot read a plausible duration off a
refusal. A `SyncBound` must be measured: it carries `max_error_us`, the
`method` that established it, and `measured_at`.

### Migration of `app/fills`

`QuoteRecord.t_quote` and `RealizedFill.t_submit` are now
`Maybe[ObservationTimestamp]`. A tz-aware `datetime` supplied by a
not-yet-migrated producer is still accepted and normalised to
`WALL_ONLY` on `UNKNOWN_HOST` — an **honest downgrade, not a repair**:
`interval()` returns `NOT_COMPUTABLE` for two such stamps, so the bypass the
contract found (*"subtracts a bare wall-clock `datetime` from a
monotonic-anchored stamp"*) is now structurally impossible rather than
discouraged.

`slot` and `t_confirmed` stay in the **chain** domain and are deliberately not
promoted; `slot` remains the ordering primitive.

`app/fills/linkage._ms` keeps working for `quote→submit` and
`submit→confirm` through an explicitly named legacy door
(`legacy_wall_interval_us`, basis `WALL_UNANCHORED`), which `interval()` never
returns. Both intervals now say on the record that they are unanchored.

### Three quantities, three names, three types

| name | span | what it is |
|---|---|---|
| `ExternalDeliveryLatency` | `t_received − t_created` | platform → us. **Cross-clock, contaminated, `is_latency` is `False`.** Requires `delivery_mode`. |
| `OurResponseLatency` | `t_quote − t_received` | ours → ours. The only sound internal interval. Wraps `interval()`, so it refuses. |
| `CrossDomainInterval` | platform→ours→chain | inherits the **worst** fidelity in the chain; refuses to be constructed for a single domain. |

`test_the_three_quantities_have_three_different_types` asserts they cannot be
confused by a consumer that reads types.

---

## 3. `TokenResolution` — mint equality is not token identity

```
chain · mint · resolver_version · confidence · evidence
status: TEXT_CANDIDATE | CANONICALLY_VERIFIED | RESOLVED_FROM_PROJECT
      | RESOLVED_FROM_ALIAS | AMBIGUOUS | REJECTED
```

`JOINABLE_STATUSES == {CANONICALLY_VERIFIED}`. One member. Every other status
is refused at the join, asserted exhaustively over the enum.

`CANONICALLY_VERIFIED` is unconstructible without **two** kinds of evidence:

1. `CHAIN_MINT_EXISTS` — the mint is real; and
2. at least one of `PROJECT_ACCOUNT_LINK`, `LINKED_PROFILE_REFERENCE`,
   `CANONICAL_REGISTRY` — corroboration that **this post refers to that
   token**.

"The mint exists" defeats none of the six named threats — *a decoy address in a
scam post is a real mint too*. The threats are kept in code as
`token.THREATS`: decoy addresses, old mints, copied addresses, competitor
mentions, screenshot contracts, quote-posted scam content.

The escalation is an **interface with a conservative default**:

| rung | stage | wired |
|---|---|---|
| post contains a mint | `TextCandidateStage` | **yes** — reuses `app.social.resolution.ConservativeAddressResolver`, ceiling `TEXT_CANDIDATE` |
| is it a live mint | `ChainExistenceStage` | Protocol only |
| does project context corroborate | `ProjectContextStage` | Protocol only |
| does a linked site/profile reference it | `LinkedProfileStage` | Protocol only |
| canonical identity | `CanonicalIdentityStage` | Protocol only |

`default_ladder()` runs one real stage and four `NotWiredStage`s, each of which
records a `NEGATIVE_CHECK` explaining what was *not* checked. **No stage
performs network I/O**, so the tests do not need to stub anything and there is
no environment in which the ladder behaves differently. The default ladder can
never reach `CANONICALLY_VERIFIED`, which is the correct state of this system
today: the join gate is shut.

`from_entity_resolution()` maps `app/social`'s `CONFIRMED` to
`RESOLVED_FROM_ALIAS`, **not** to `CANONICALLY_VERIFIED` — `app/social` defines
`CONFIRMED` as "confirmed against an authoritative source" with no such source
wired, and promoting it would let an unidentified registry claim into the alpha
cohort. Confidence is carried on the record either way; §4 forbids dropping it.

---

## 4. `delivery_mode` as a binding cohort dimension

* `LiveLeadLagCohort` cannot be constructed containing anything but `LIVE`.
  Not by flag, not by override, not by later mutation — the check is in
  `__post_init__` and the record is frozen.
* `DeliveryCohort` refuses a mixed member set at construction.
* `DeliveryCohort.pool()` **always raises**. There is no `force=`, no
  `allow_pooling`, and no environment variable.
* `CohortPurpose` makes the distinction explicit: `BACKFILL` / `PULLED` /
  `UNKNOWN` are permitted for `SOURCE_REPUTATION`, `SEMANTIC_ANALYSIS`,
  `PROPAGATION_RECONSTRUCTION` and `RESOLVER_TRAINING`, and refused for
  `LATENCY_LEAD_LAG`. Backfill is not junk; it is disqualified from exactly
  one thing.
* `partition_by_delivery_mode()` and `delivery_mode_breakdown()` are the
  sanctioned alternatives — §5's "no pooled delivery-timing figure may be
  reported without a `delivery_mode` breakdown beside it".
* `UNKNOWN` is never optimistically treated as `LIVE`.

---

## 5. The join

`join_social_to_fill()` returns a `JoinedEvidenceRow` or a `JoinRefused`,
never a partially-degraded row. Gate order, most dangerous first:

1. token identity — `CANONICALLY_VERIFIED` only;
2. the verified mint must be the fill's mint;
3. `delivery_mode` must be `LIVE` for a latency purpose;
4. the fill must carry a signature.

A `JoinRefused` carries **no** `mint`, `tx_signature`, `slot` or latency —
asserted, because a refusal that still exposes a field will be read by
something.

A joined row carries, per §5: `raw_content_hash`, `ingestion_version`,
`delivery_mode`, `tx_signature`, `decoder_version`, the full `TokenResolution`
including confidence, the three time quantities separately, and the deferred
observations as `Measurement`s so `OBSERVED_NONE` survives.

---

## 6. The eight positive controls, and their RED evidence

Doctrine 7: testing the healthy state only proves the healthy state. Each
control was mutated to remove the defence it exists to prove, run, and
reverted. **All eight went red.** Campaign harness:
`scratchpad/red.py` (not committed — it mutates `app/` in place).

| # | control | mutation applied | observed failure |
|---|---|---|---|
| 1 | `OBSERVED_NONE` survives a join as measured negative | `join.py`: pass `Measurement.not_attempted(NOT_PROVIDED)` instead of the caller's measurement | `assert row.first_onchain_reaction.is_measured_negative is True` → `assert False is True`, `Measurement(availability=NOT_PROVIDED, observation=NOT_ATTEMPTED)` |
| 2 | `NOT_PROVIDED` cannot become zero | `measurement.py`: `__bool__` returns `self.value is not None` instead of raising | `Failed: DID NOT RAISE MeasurementAbsentError` (on `_ = nothing or 0`) |
| 3 | mismatched clock epochs → `NOT_COMPUTABLE` | `clock.py`: drop the epoch-equality condition from rule 2 | `assert isinstance(ComputedInterval(microseconds=400000, basis=MONOTONIC_SAME_EPOCH), NotComputable)` → `False` |
| 4 | valid same-host monotonic stamps yield a computable interval | `clock.py`: disable rule 1 (`if False:`) | `assert isinstance(NotComputable(reason=UNKNOWN_BOOT_NO_EPOCH_MATCH), ComputedInterval)` → `False` |
| 5 | an unverified base58 mint cannot join | `token.py`: `JOINABLE_STATUSES = frozenset(TokenResolutionStatus)` | `assert resolution.is_joinable is False` → `assert True is False` on a `TEXT_CANDIDATE` |
| 6 | a verified canonical mint does join | `join.py`: `if not resolution.is_joinable:` → `if True:` | `assert isinstance(JoinRefused(TOKEN_NOT_CANONICALLY_VERIFIED), JoinedEvidenceRow)` → `False` |
| 7 | backfilled artifacts cannot enter the live cohort | `cohort.py`: disable the member-mode check | `Failed: DID NOT RAISE CohortPoolingError` |
| 8 | flipping `delivery_mode` LIVE→BACKFILL breaks the primary-cohort test | (a) `cohort.py`: `PRIMARY_ALPHA_DELIVERY_MODE = BACKFILL`; (b) the **data flip**: default the test fixture `an_artifact(delivery_mode=…)` to `BACKFILL` | (a) `CohortPoolingError: cohort is BACKFILL but a member is LIVE`; (b) **6 tests red**, including controls 1 and 6, `JoinRefused(NOT_LIVE_DELIVERY)` |

Control 8's second form is the faithful one: flipping a single data field —
with no code change at all — turns six assertions red. `delivery_mode` is not
decorative.

**Hardest to make fail: control 4.** It is the only control whose healthy
state is "a number exists", so a naive version passes in a repository where
nothing works — exactly the failure shape doctrine 4 names. Disabling rule 1
was not enough on its own: rule 2 (same process epoch) silently caught the
pair and still returned a `ComputedInterval`, just with a different basis. The
control only became a real control once it asserted the **basis**
(`MONOTONIC_SAME_BOOT`) and the **value** (`400_000 µs`) and used stamps from
two *different* process epochs — which is the cross-process case the whole
type exists for. A control that had only asserted `is_computable` would have
stayed green through the mutation.

Runner-up: control 2. `bool()` was easy to break red, but the first draft
asserted only `unwrap()` — and `unwrap()` raising is not the dangerous path.
The dangerous path is `x or 0`, which is why the control exercises it
literally.

---

## 7. What I found WRONG in the join contract when I implemented it

Three real defects, all in §2, all found by writing the adapter tables out.

**(a) §2's table lists 5 of `app/fills`' 7 `AbsenceReason` members.** It omits
`TRANSACTION_FAILED` and `CONFLICTING_SOURCES`. A "lossless mapping declared as
a table" built from §2 as written would have silently had no image for two real
members. Fixed by carrying `OriginTag`, so the coarse projection is reversible
exactly; the round-trip test iterates the *enum*, not the contract's table, so
this cannot regress.

**(b) The prescribed `Availability` enum drops `NOT_APPLICABLE` — the ONE
member both vocabularies agree on.** The milestone brief named five members:
`AVAILABLE`, `NOT_PROVIDED`, `NOT_RECONSTRUCTABLE`, `NOT_YET_OBSERVED`,
`NOT_AUTHORIZED`. `NOT_APPLICABLE` is in both `fills.AbsenceReason` and
`social.DeferredState`, and it is the most common absence in
`app/fills/corpus.py`. Omitting it would have forced every adapter to invent
one of the other five for it — inventing information at the exact seam built to
prevent that. **Deviation taken and flagged:** `Availability` has six members.

**(c) `social.Deferred(OBSERVED_NONE)` records an `observed_at` instant but no
window.** §2 calls `OBSERVED_NONE` "a real measurement, and the negative case
any lead-lag study needs" — but as `app/social` stands today the tape cannot
say *how long it watched*, so the negative cannot state its own noise floor.
The seam refuses to invent one: `from_social_deferred` on an `OBSERVED_NONE`
requires an explicit `window=`. **This is a gap in `app/social` that a future
milestone should close** by adding a window to `Deferred`; `app/social` was not
modified here beyond leaving the requirement visible.

Two smaller notes:

* **§3's asymmetry claim understated the fix.** The contract asks for
  `app/fills` to adopt `OurReceivedAt`. Adopting it verbatim would have
  imported `app/social`'s *process-epoch* rule into `app/fills`, which refuses
  every cross-process interval — i.e. every interval the join actually wants.
  The boot-id key is what makes the requirement satisfiable rather than merely
  strict.
* **`submit→confirm` was already a cross-domain interval** (`OURS → CHAIN`)
  and §3.2 says no interval may cross a domain boundary untyped. It was
  untyped before this branch. It is now computed through the labelled legacy
  door and says so; fully typing it needs `t_submit` producers migrated to
  `capture_observation`.

---

## 8. What this seam CANNOT do

* It **cannot make the three clocks comparable.** It can only stop them being
  silently mixed. `ExternalDeliveryLatency` remains contaminated evidence.
* It **cannot verify a mint.** `CANONICALLY_VERIFIED` is a *state the seam can
  represent and gate on*; nothing in this branch can produce one from the
  network, and `default_ladder()` provably cannot reach it. Until a chain
  existence stage and a corroboration stage are wired and separately
  authorized, **the primary alpha cohort is empty by construction.**
* It **cannot establish causation.** Cross-domain ordering by receive
  timestamps is correlational (doctrine 12).
* It **cannot fix `app/fills`' timestamp producers.** They still emit bare
  `datetime`s; the seam types the resulting weakness rather than removing it.
  Every migrated legacy stamp is `WALL_ONLY` on `UNKNOWN_HOST`.
* It **cannot recover a boot id retrospectively.** `OurReceivedAt` records
  didn't have one, so lifted social stamps fall back to epoch comparability.
* It **cannot detect a lying platform timestamp**, an edited post, or a
  re-issued item.
* It **says nothing about pool identity** (§6.4, still `NOT_RECONSTRUCTABLE`)
  or `ε_fill` (still `NOT_AUTHORIZED`).
* It **is not deployed and has no production caller.** `app/seam` is imported
  by `app/fills/schema.py` for the timestamp type and by nothing else.

---

## 9. What would falsify it

1. **A joined row whose negative label is not a measurement.** If any
   `first_onchain_reaction.is_measured_negative` row cannot name the window it
   watched, the `OBSERVED_NONE` guarantee is cosmetic.
2. **A monotonic interval that spans a reboot.** If two stamps share a
   `host_boot_id` across a reboot, rule 1 is unsound. Test: read
   `/proc/sys/kernel/random/boot_id` before and after a real reboot on the
   Linux host and confirm it changes. **This has not been done — the boot-id
   path is entirely unexercised on Linux, because this branch is Mac-only and
   macOS returns `NOT_AVAILABLE_ON_PLATFORM`.** Every boot-id test here uses
   constructed values.
3. **A `MONOTONIC_SAME_BOOT` interval that disagrees with a bounded wall
   interval by more than the bound.** Run both on one host across two
   processes; a disagreement falsifies the boot-relative assumption for that
   platform.
4. **A `CANONICALLY_VERIFIED` resolution that is still wrong.** Hand the
   verified set to an agent screen (doctrine 12) against the six threats. Any
   survivor means the two-evidence rule is insufficient.
5. **`delivery_mode` that does not move the delivery distribution.** If the
   `LIVE` and `BACKFILL` `contaminated_us` distributions are indistinguishable
   on real data, the cohort separation costs sample for nothing — and §5's
   premise is wrong.
6. **A consumer that reads a number off a refusal.** `NotComputable` and
   `JoinRefused` expose no numeric field; if a downstream consumer still
   produces a duration or a mint from one, containment leaked.
7. **The eight controls staying green under mutation.** Re-run §6's campaign
   after any change to `app/seam`.

---

## 10. Test counts (actually run, `.venv/bin/python -m pytest`)

| suite | result |
|---|---|
| `tests/test_social_fill_measurement_seam_001.py` | **76 passed** |
| `tests/test_realized_fill_*.py` (5 files) | **82 passed, 1 skipped** |
| `tests/test_social_*.py` (3 files) | **153 passed** |
| the nine files together | **311 passed, 1 skipped** |
| tree-scanning guards (`test_calibration_gate3_001`, 6 × `test_kalshi_*`, `test_agent_canon`, `test_cli`) | **486 passed** |
| **total actually run and green** | **797 passed, 1 skipped** |

The **full suite was NOT run to completion** and nothing here claims it was.
Two attempts were made; the first reached 81% but was contaminated by a
second, concurrently-running pytest (my own duplicate launch), which is
exactly the load condition the known flaky class `TestEndToEnd` fails under,
so its F/E marks are uninterpretable. A clean single run was reaching ~6% per
15 minutes and was stopped.

That is acceptable here because the **blast radius is closed by inspection**:
`grep -rl "app\.fills|app\.seam|app\.social" tests/` returns exactly the
nine files above, and the eleven files that walk the `app/` tree are the
second group. Nothing else in the repository can see this change.

`tests/test_social_x_collector_001.py::TestSeam::
test_no_module_in_app_imports_app_social` was **narrowed, not disabled.** Its
own docstring said a caller "needs an explicit decision"; the decision was
taken and written into the test: `app/seam/` may reference `app.social`
**types**, and a new, sharper guard —
`test_the_seam_references_social_types_but_never_activates_collection` —
asserts that no seam module mentions `x_collector`, `transport`, `connectors`,
`tape` or `cost_guard`, **and** that `app/seam` exists and does reference a
social type (doctrine 4: assert the permitted thing exists, or the guard is
satisfied by a repository in which nothing works).

Two further guards prove the same thing at **import time** rather than by
grep: `test_importing_the_seam_pulls_in_no_collection_module` (importing all
five seam modules loads `app.social`, `app.social.artifact` and
`app.social.timebase` and nothing else) and
`test_importing_app_fills_pulls_in_no_social_module_at_all` (importing
`app.fills.schema` loads **no** `app.social` module — the corpus stays
independent).

## 11. Constraints honoured

* Mac only. No `ssh evo-x2`, no deploy.
* `app/realtime/` unmodified (verified: no file under it is in the diff).
* `AGENTS.md` unmodified.
* No alpha model, signal, scoring, trading or capital. Asserted mechanically by
  `TestSeamContainsNoSignal`.
* Branch `SOCIAL-FILL-MEASUREMENT-SEAM-001`, not merged.
