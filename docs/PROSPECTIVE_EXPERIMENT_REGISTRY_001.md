# PROSPECTIVE-EXPERIMENT-REGISTRY-001 — research pre-registration

**Status:** implemented and reviewed; registration state in §10.

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
| timing | `start_condition`, `start_time`, `end_condition`, `evaluation_horizons` |
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
| `start_time` in the past | a prospective experiment cannot admit existing forecasts |
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

_See §11 for the registration decision._

Three drafts are authored and validate cleanly:

| experiment | class | domain | primary metric | floor | digest |
|---|---|---|---|---|---|
| `baseball-prospective-calibration-stability` | prospective_calibration | sports_baseball | `brier_skill_vs_base_rate` | 500 | `9872aafd…` |
| `soccer-prospective-reliability` | domain_reliability | sports_soccer | `brier_skill_vs_base_rate` | 300 | `8435eb5a…` |
| `tennis-base-rate-falsification` | domain_reliability | sports_tennis | `brier_skill_vs_base_rate` | 200 | `59586978…` |

All three are **confirmatory**, all draw only on forecasts created at or after
`start_time`, and all declare the base-rate benchmark computed *within their own
matured members* rather than inherited from the historical aggregate.

No experiment was authored for politics (n=30) or any other thin domain. The
milestone asked for a small portfolio, not a sweep, and manufacturing a
hypothesis for a domain that cannot reach a floor would be exactly the kind of
box-ticking this registry exists to prevent.

## 11. Registration decision

_Recorded after review and deployment._

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
