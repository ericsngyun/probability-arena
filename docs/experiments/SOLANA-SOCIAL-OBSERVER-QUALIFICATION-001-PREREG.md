# SOLANA-SOCIAL-OBSERVER-QUALIFICATION-001 — preregistration

**Status: FROZEN, NOT RUN. No social connection has been opened.**
Written 2026-08-27, **before any live count exists**. That ordering is the
point: once ingestion starts, source selection becomes part of the experiment.

Read-only. No trading, no capital, no signer.

---

## 1. The question, and only this question

> Can the system obtain enough prospective, correctly attributed, clock-valid
> social observations to support a later alpha experiment?

**Not** whether those observations predict price. Nothing in this milestone
computes a return, markout, price response, win rate, source score, token
ranking or trading output, and the funnel module has no field for one.

## 2. The frozen funnel outputs

Preregistered now, so the reported quantities cannot be chosen once their
values are visible:

> N_received → N_mint_candidates → N_authority_resolved → N_chain_verified
> → N_canonical_verified → N_clock_computable → N_joinable

Reported beside them: per-stage losses, the largest single loss, and typed
refusal counts. Every artifact terminates in the next stage or a typed refusal;
an outcome recording neither raises at construction.

**The survival rate `P(joinable | received)` and the absolute count of
independent joinable events are the deliverables.** They are not a result about
markets — they are a result about whether a market experiment is possible.

## 3. Source selection — the prohibition, frozen before any count

Sources may be selected for **protocol, access and diversity** reasons:

* they publish contract addresses in a parseable form;
* access is authorized and rate-limited in a way we can afford;
* together they exercise the paths the funnel must handle — direct mint
  publications, no-mint posts, quotes and forwards, migrations, conflicting
  addresses, duplicated announcements, and more than one delivery mode.

Sources may **NOT** be selected because they:

* historically called profitable tokens;
* generated strong returns;
* appear to have "alpha";
* have high follower counts or engagement;
* are believed to be "good callers".

**No historical return, price series, or performance anecdote may enter source
selection.** A source universe chosen on past profitability would make the
funnel a measurement of our own hindsight rather than of the pipeline.

Target size **15–25 sources** across classes: official project accounts that
sometimes publish contract addresses · launchpad/ecosystem announcement
sources · exchange/listing announcement sources · a few highly active
project/community sources · authorized Telegram/Discord sources where access
already exists.

The frozen list, its per-source justification, and the class each satisfies are
recorded before the first connection.

## 4. Cost envelope, frozen before connecting

Per provider, declared in advance:

| field | meaning |
|---|---|
| `provider` | which service |
| `source_count` | how many sources on it |
| `daily_spend_ceiling` | hard cap |
| `monthly_spend_ceiling` | hard cap |
| `max_artifacts_per_day` | ingestion cap |
| `reconnect_policy` | what happens on a dropped stream |
| `backfill_policy` | whether backfill is fetched at all |
| `on_exceeded` | **stop** or **degrade**, named in advance |

If a ceiling is reached, collection stops or degrades **by the predeclared
rule**. Discovering mid-run that a rule set is ingesting tens of thousands of
irrelevant posts is a budget failure, not a finding.

## 5. LIVE and BACKFILL cannot be pooled

Only `delivery_mode == LIVE` counts toward **latency and funnel qualification**.

Backfill may exercise parsing, extraction and identity resolution — it is
useful for coverage of rare shapes — but it **never** counts toward prospective
delivery performance, because its receipt timestamp measures when we fetched
it, not when it arrived.

`app/seam/fill_seam.py` already refuses anything but exact `LIVE` with
`DELIVERY_NOT_LIVE`, and the cohort machinery already refuses to pool the two.

## 6. Positive controls, so that zeroes mean something

A funnel that reports **0 authoritative sources** is ambiguous: it may mean the
sources are unattested, or that the authority code path was never exercised.
The qualification therefore includes:

* at least one source whose reciprocal attestation is **known to resolve**, so
  `AUTHORITATIVE` is reachable and a zero elsewhere is informative;
* at least one artifact shape known to produce a mint candidate, so
  `N_mint_candidates = 0` cannot be a parser failure hiding as a market fact;
* a deliberate quoted/forwarded artifact, so the `QUOTED` refusal path is
  exercised rather than merely available.

Without these, every stage count is consistent with "that code never ran".

## 7. What would make this qualification a failure

* an artifact that reaches no stage and no refusal — silence is not a stage;
* a joinable event whose canonical mint cannot be traced to its evidence;
* a `pipeline_us` computed across hosts or boot epochs;
* any pooling of LIVE and BACKFILL in a latency figure;
* a source that entered the universe on performance grounds.

## 8. What follows, and what does not

Next is `SOLANA-ALPHA-FEASIBILITY-001` — a bounded prospective window
measuring joinable events/day, unique canonical mints, distinct authoritative
sources, duplication and propagation structure, `L_delivery`, `L_pipeline`, and
chain/quote observation availability. **Still with no forward returns.**

`SOCIAL-LEAD-LAG-001` is designed **only after** feasibility reports a corpus
size, because the experiment's design depends on it:

> 50,000 received → 12 joinable, and 5,000 received → 800 joinable, are
> different projects. They imply different source-universe sizes, window
> lengths, infrastructure budgets and statistical designs.

**Source reputation is not built yet.** It must eventually be estimated as
incremental prospective information — `R_i(h) = E[r | source_i, event, state] −
E[r | state]` — never from follower count or historical anecdote, and that
requires a prospective corpus that does not yet exist.
