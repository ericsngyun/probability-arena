# PROSPECTIVE-EXPERIMENT-REGISTRY-001 — research pre-registration

**Status:** implemented, reviewed, **dark-deployed to EVO-X2 2026-08-06**.
**Registration deferred — §11.**

The post-drain baseline made prospective research possible for the first time —
the scored sample is finally the population rather than an id prefix. This
milestone builds the governance that makes prospective research *credible*.

---

## 1. Motivation

The threat this addresses is not an adversary. It is us, six weeks from now,
looking at a null result and noticing that the finding would be significant if
the window started a week later, or if tennis were excluded, or if ECE were the
primary metric instead of Brier skill. Every one of those edits is individually
defensible and collectively fatal.

This milestone series has already produced two live examples of why:

- soccer's Brier of **0.003297** on n=34 was reported as a domain result. On a
  representative sample it is 0.157593. Nobody cherry-picked it — the selection
  did it silently.
- tennis's negative skill has now been measured three times at three sample
  sizes and moved from −0.1427 to −0.0368. Without a declared floor and stopping
  rule, whichever reading someone stopped at would have become "the" result.

A registry cannot make research correct. It can make it *hard to quietly change
the question after seeing the answer*.

## 2. Immutable source of truth — Git-backed manifests, no database

Option 1 from the milestone, chosen over SQLite and the hybrid.

The decisive argument is the threat model. A registry entry must be harder to
change than the thing it governs. A row in `probability_arena.db` — a file this
project writes to every six minutes, prunes on a retention policy, and has
already reconciled duplicates in once — is a weak barrier against a well-meaning
edit. A committed file cannot change without producing a diff, an author and a
timestamp, in a repository that already treats history as append-only and
requires review to alter.

The hybrid was rejected on the milestone's own terms: it is preferred *only*
when the SQLite projection is demonstrably non-authoritative and rebuildable.
There is currently nothing to query that `list` and `status` cannot answer by
reading a few hundred small files. Buying an index we do not need at the cost of
coupling governance to the production database would trade real immutability for
convenience. If querying ever becomes the bottleneck, a rebuildable projection
can be added then, with manifests still authoritative.

```
experiments/<experiment_id>/manifest.json     # the declaration, written once
experiments/<experiment_id>/events.jsonl      # append-only state log
experiments/<experiment_id>/results/          # evaluations, added at maturity
```

`manifests/` holds drafts before registration; `experiments/` holds registered
work. The separation is deliberate — a draft is meant to be edited.

## 3. Manifest schema

37 required fields, spanning identity, hypothesis, population, features, data
policy, metrics, floors, and safety. The full list is `REQUIRED_FIELDS` in
`app/services/experiment_registry.py`; the ones that carry weight:

| group | fields |
|---|---|
| hypothesis | `hypothesis`, `null_hypothesis`, `exploratory_or_confirmatory`, `experiment_class` |
| population | `market_population`, `domain`, `forecast_family`, `forecast_version`, `inclusion_rules`, `exclusion_rules` |
| timing | `start_condition`, `end_condition`, `evaluation_horizons` (`start_time` is registry-assigned) |
| metrics | `primary_metric` (exactly one), `secondary_metrics`, `declared_baselines` |
| floors | `sample_floor`, `domain_sample_floors`, `minimum_matured_fraction` |
| data policy | `missing_data_policy`, `canceled_void_policy`, `conflict_policy`, `stale_score_policy` |
| governance | `multiple_testing_policy`, `stopping_rule`, `invalidating_conditions`, `known_limitations`, `safety_boundary` |

`HYPOTHESIS_FIELDS` marks the 26 that define the question. Changing any of them
after registration changes the experiment, and the digest detects it.

**Digest.** SHA-256 over canonical JSON (sorted keys, tight separators),
**excluding** registry-assigned fields. That exclusion matters: the digest a
reviewer verifies *before* confirming must equal the one stored *after*, or
showing it for confirmation is theatre.

## 4. Immutable snapshot references (B3)

Manifest text alone cannot reconstruct an experiment — the same words scored by
different code produce different numbers. At registration the registry captures:

- content digests of the population, inclusion, exclusion, feature, signal,
  baseline and primary-metric definitions;
- `forecast_family` and `forecast_version`;
- the repository commit;
- SHA-256 of the four evaluation-code files (`forecast_reliability`,
  `calibration`, `forecast_scorability`, `outcome_coverage`);
- SHA-256 of `PROJECT_CANON.md` and `SAFETY_BOUNDARIES.md`.

No production data is snapshotted — the rules to rebuild membership, not a
frozen copy of the rows. `status` reports `evaluation_code_drift`: **disclosure,
not tampering.** Scoring code improves (the Phase A fix in this very milestone
is an example), but a result computed after such a change is not directly
comparable to the declaration that preceded it, and that must be visible rather
than discovered afterwards. Drift leaves the hypothesis digest intact.

## 5. State machine

```
draft ─► registered ─┬─► collecting ─┬─► matured ─► evaluated ─► retired
                     │               │
                     └──────────────►└──► invalidated ─► retired
```

Append-only, and **no edge returns to `draft`**. State is reconstructed from
`events.jsonl`, never read from the manifest's convenience `state` field; if
they disagree the events win, because they are the append-only artifact.

A transition is refused outright when the manifest digest no longer matches the
one recorded at registration — an experiment whose declaration has changed
cannot advance.

## 6. Anti-p-hacking controls

Rejected outright:

| control | why |
|---|---|
| multiple primary metrics | how a null result becomes a positive one |
| missing `sample_floor` | permits stopping wherever the number looks best |
| missing `stopping_rule` | same, by another route |
| missing `declared_baselines` | a metric with no baseline cannot support anything |
| outcome-derived inclusion | `best_performing`, `outcome settled yes`, `score_status` |
| future-information features | `settlement`, `final_price`, `closing_price` |
| author-supplied `start_time` | the registry stamps it at confirmation, so prospectivity holds by construction rather than by promise |
| duplicate `experiment_id` | identities are never reused |
| overwriting a registered manifest | refused before any write |
| confirmatory multi-hypothesis with no multiple-testing policy | undisclosed multiplicity |
| trading vocabulary | this registry governs measurement only |
| secret-bearing field names / credential-shaped values | manifests are committed |
| path traversal in `experiment_id` | writes attacker-chosen content into a committed tree |

Flagged, not rejected: very thin sample floors; domains with known poor
calibration (tennis, politics); declared dependency on unresolved provider
coverage.

**Two of these checks were wrong in the first draft and are fixed rather than
loosened.** The trading-vocabulary scan rejected `safety_boundary`, whose whole
purpose is to say "no execution or capital behavior" — a substring scan cannot
tell an assertion from its negation, so four boundary fields are exempt from
that scan only (still scanned for secrets), with the exemption list asserted in
a test so it cannot grow quietly. And the credential detector used
`[A-Za-z0-9_-]{32,}`, which matched every long identifier in the project and
rejected the experiment IDs themselves; a check that fires on ordinary correct
input gets deleted rather than tightened, so it is now scoped to real credential
shapes with false-positive tests pinning the project's own vocabulary as benign.

## 7. Exploratory versus confirmatory

Explicit and required. Confirmatory work is held to the full bar: one primary
metric, fixed population, fixed stopping rule, fixed floor, a multiple-testing
policy when several hypotheses are in play, and a `minimum_matured_fraction`.
Exploratory work is not — it may roam — but it can never be promoted to
confirmatory evidence, and a failed confirmatory experiment stays in the log
permanently via `invalidated → retired`. Nothing is deleted.

## 8. Evaluation contract

A result must report registered vs actual population, excluded counts by
declared rule, unexpected exclusions, matured/pending/unscorable counts, the
primary metric against its declared baseline with a confidence interval,
sample-floor status, secondary metrics, composition, domain and
forecast-version concentration, missingness, protocol deviations, invalidating
events, and a verdict from exactly:

`supports_hypothesis` · `does_not_support_hypothesis` ·
`inconclusive_sample_floor` · `invalidated_protocol_deviation` ·
`invalidated_data_quality` · `still_collecting`

`profitable`, `tradeable`, `buy`, `sell`, `edge` and `opportunity` are not
available, in verdicts or in manifest prose. Evaluation is refused before the
`matured` state.

## 9. Tests and reviews

70 registry tests plus 18 for the Phase A directional fix; full suite **2,627
passed**. Coverage spans all 35 required areas: validation and rejection paths,
digest determinism and key-order independence, dry-run purity, registration,
duplicate rejection, immutability detection, append-only transitions, invalid
transitions, state reconstruction from events, failed-experiment preservation,
evaluation-before-maturity refusal, path traversal, secret detection precision
in both directions, text/JSON parity, AST safety scans, no migration, no
database coupling, no timer, no provider imports.

## 10. Registered experiments

**None. Registration is deferred — see §11.**

Three drafts are authored in `manifests/` and validate cleanly:

| experiment | class | domain | primary metric | floor | digest |
|---|---|---|---|---|---|
| `baseball-prospective-calibration-stability` | prospective_calibration | sports_baseball | `brier_skill_vs_base_rate` | 500 | `af207c21…` |
| `soccer-prospective-reliability` | domain_reliability | sports_soccer | `brier_skill_vs_base_rate` | 300 | `d706f54e…` |
| `tennis-base-rate-falsification` | domain_reliability | sports_tennis | `brier_skill_vs_base_rate` | 200 | `ac58890c…` |

All three are **confirmatory**, all declare a base-rate benchmark computed
*within their own matured members* rather than inherited from the historical
aggregate, and none carries an author-supplied `start_time` — the registry
stamps it at confirmation, so prospectivity holds by construction.

No experiment was authored for politics (n=30) or any other thin domain. The
milestone asked for a small portfolio, not a sweep, and manufacturing a
hypothesis for a domain that cannot reach a floor would be exactly the
box-ticking this registry exists to prevent.

## 11. Registration decision — DEFERRED

An independent governance review recommended against registering today, and it
was right. Its blocking findings are fixed; two that determine whether this is a
control or a ritual are not, and registering real experiments against a registry
whose evaluation side does not yet enforce anything would manufacture the
appearance of governance without the substance.

### Fixed

| # | finding | resolution |
|---|---|---|
| H1 | All three manifests were **already invalid** — hand-written `start_time` had passed within an hour of authoring, and re-dating would have changed every digest printed in this document | `start_time` is now registry-assigned; the time bomb is structurally impossible and this digest table is generated from source |
| H2 | `start_time` was **not required**, so omitting it skipped the only prospectivity check entirely | Author-supplied `start_time` is now rejected outright |
| H3 (partial) | `events.jsonl` was plain lines: truncating one rolled `invalidated` back to `matured` and let the experiment advance again | Events are hash-chained with `prev`/`seq`; `verify_immutability` fails on a broken chain |
| H4 | Pinning **silently degraded to null** when run from any other directory, after which drift reported a permanent clean bill of health; `commit=None` was accepted | `repo_root` derives from the module, a missing pinned file raises, and `--confirm` without a commit is refused |
| M2 | The vocabulary blocklist rejected "in order to", "alphabetical", "acknowledged limitation" and "non-profit" — ordinary prose — while accepting "identifies mispricing we can act on" | Word-boundary matching, plus the paraphrases the review demonstrated |
| M7 | The CLI raised raw tracebacks on a corrupt registry — precisely when a clean diagnostic matters | Both commands return 2 with a readable message |
| L1 | The "partition" summed to 0.9999 | The largest share absorbs the rounding residual |

### Not fixed — why registration waits

- **M1 — the leakage guard is a prose blocklist and is bypassed by paraphrase.**
  The review showed "include forecasts in the cohort that beat the benchmark"
  passing: *exactly* the post-hoc selection this exists to stop. It also found
  that this milestone's own tennis manifest ships `"member is not scored_current
  at evaluation"`, which is semantically identical to a phrasing the tests
  assert is rejected. The right shape is an enumerated predicate vocabulary with
  free text demoted to a non-operative `rationale`, not a longer blocklist.
- **M5 — nothing connects the registry to evaluation.** There is no result-write
  path, so `sample_floor`, `stopping_rule` and `primary_metric` are recorded
  promises that no code checks. The missing piece is
  `experiment-registry-record-result`, refusing a result whose metric name is
  not the declared primary, whose n is below the floor, or whose experiment is
  not `matured`.

Until M1 and M5 land, the registry is a good filing cabinet with a strong lock
and no inspector. Filing three real experiments in it now would let us cite
pre-registration we have not actually enforced.

## 12. Limitations

- **The leakage guard is a token blocklist.** It catches the obvious phrasings
  and will not catch a leaky rule expressed in novel words. It is a speed bump
  plus a review prompt, not a proof, and should not be described as one.
- **`stopping_rule` is declared text, not enforced code.** The registry records
  what was promised; it cannot stop someone evaluating early. What it can do —
  and does — is make the deviation visible against an immutable declaration.
- **`start_time` is validated against wall clock at registration**, so a
  manifest registered with a future start is prospective by construction, but
  the registry does not itself gate which forecasts an evaluator later reads.
- **Baseball dominance persists.** It is 85% of the scored population, so the
  baseball experiment and any aggregate move together and are not independent.

## 13. Rollback

Delete the `experiments/` directory and revert the commits. No migration, no
schema change, no production database write, no MarketOps behaviour change —
the registry is inert with respect to the running system. Nothing that has been
registered is *deleted* by a rollback of the code; the manifests remain in
history, which is the point.

## 14. Future executable-price boundary

This registry deliberately cannot express an execution, P&L, order or trading
experiment class, and its verdict vocabulary cannot describe a result as
profitable or tradeable. If executable-price research is ever separately
authorized, it must arrive as its own milestone with its own review — and the
right shape is a *new* experiment class added deliberately, not a widening of
these definitions. A registry whose boundaries drift is not a boundary.


---

## 15. EVO-X2 dark deployment — 2026-08-06T01:2xZ

Mac = origin = EVO-X2 = `cb242f1`, Alembic **0027**, fast-forward only, no
restart, no MarketOps hook, no timer.

**Diff audited before deployment** (`92f9e2a..cb242f1`, 9 files): CLI, registry
service, reliability directional clarification, three draft manifests, two test
files, one document. Zero changes to `alembic/`, `app/config.py`,
`app/models.py`, `marketops.py`, `outcomes.py`, `calibration.py`, `app/adapters/`,
`infra/`, any `.service`/`.timer`, or `.env`. The only EV/trading-vocabulary
matches in the diff are the module disclaimer and the blocklist that rejects
that vocabulary.

**CLI validation on EVO, temporary inputs only:** `list` on an empty registry;
all three drafts `VALID` with digests **byte-identical to Mac**
(`af207c21…`, `d706f54e…`, `ac58890c…`); `register` dry run reported
`persisted false` and left the temporary directory **empty**; `git status`
unchanged. Zero provider calls, zero database writes, zero events created.

**Runtime health after deployment:** cycles 7971–7973 all `ok` with
`stage_errors={}`; `distinct scored = 13,233 = total forecasts` (the coverage
repair is still fully drained and tracking arrivals); duplicate current scores
**0**; backup **healthy**.

### One honest correction to the post-drain record

Lifetime `database_locked` events are now **5, not 4**. The new event is
`2026-08-05T15:55:02Z` — during the drain window, hours before this deployment
and unrelated to the registry, which is inert at runtime. It was retried
(`attempt_number: 2`, `lock_wait_ms: 32033`) and committed exactly, so nothing
was lost. The post-drain baseline's "zero new lock events" was true when
measured at 13:07Z and is no longer true; the coverage repair at score limit 100
has since cost exactly one retried lock event.

### Known gap in the REGISTRY-001 CLI contract

`experiment-registry-report` was listed in the milestone's CLI contract and was
**never implemented**. `validate`, `register`, `status`, `list` and `transition`
all exist and work. This is recorded rather than quietly dropped, and belongs in
REGISTRY-002 alongside the result-recording path.
