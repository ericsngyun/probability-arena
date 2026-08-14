# SOLANA-ROUTE-OBSERVATION-001 — scope

**Status: SCOPE ONLY.** No production code, no feature flag, no migration, no
schema change, no behaviour change, no deployment, no provider call. The only
artifact of this milestone-as-written is this document. Nothing here is
implemented; nothing here is approved.

Branch base: `origin/main` @ `8790a25`. Alembic head at time of writing:
`0029_horizon_member_cohort_added_at`.

**EVO-X2 was not contacted.** The host is unreachable pending a Tailscale
re-auth, so every number in this document is either read out of this repository
(cited by file and line) or explicitly labelled as an assumption to verify.
Nothing was measured on the production host for this document.

---

## 0. Read this before the rest — the boundary finding

The standard way to answer *"if I put N dollars into token T right now, what
would I get back"* on Solana is to ask a DEX aggregator's quote endpoint. That
path is closed in this repository, and not by accident:

| what | where |
|---|---|
| "Swaps / transaction construction / signing (**Jupiter** or any DEX)" is listed as forbidden with **no implementation surface**, gated behind `WALLET-001`, itself gated on CRYPTO-002 + CRYPTO-003 | `docs/SAFETY_BOUNDARIES.md:19` |
| the same phrase in the canon declaration | `app/canon.py:101` |
| `"jupiter"` and `"swap"` are **banned identifier fragments** in the AST safety audit over `app/` | `app/services/frontier_eval.py:58-77` |
| the safety grep that must come back clean before a milestone is declared done | `AGENTS.md:40` |

Two consequences that shape everything below:

1. **The highest-fidelity non-executing estimator on Solana is
   `simulateTransaction` against a constructed swap instruction.** It requires
   transaction construction, which `docs/SAFETY_BOUNDARIES.md:19` forbids
   outright. This milestone deliberately forgoes the best available estimator.
   That is a real fidelity cost, stated here so it is not rediscovered later as
   a surprise.

2. **An aggregator quote GET is unauthenticated, carries no wallet, signs
   nothing and submits nothing — but it is a swap-router surface, and the
   boundary as written names it.** Whether to permit it is a Tier 3 decision for
   Eric, in the style of the `KALSHI-READONLY-AUTH-001` amendment
   (`docs/SAFETY_BOUNDARIES.md:21-80`), where the boundary was openly amended
   rather than quietly reinterpreted. It is **Open Question Q1** and it is not
   decided here.

There is a third boundary fact the strategic framing has to confront directly.
The target ledger is `Opportunity → PaperOrder → ExecutionQuote → PaperFill →
Position → ExitDecision → ExitQuote → RealizedPaperPnL`. **Paper trading /
simulation is a forbidden capability today** (`docs/SAFETY_BOUNDARIES.md:12`,
gated on MVP-005B acceptance; the crypto analogue is CRYPTO-003 at line 19). So
even a perfect route observation does not unlock a `PaperFill`. This milestone
is *upstream* of that gate, not through it, and it must not scaffold the
ledger's downstream rows — `docs/SAFETY_BOUNDARIES.md:84` explicitly bans
"disabled" or "placeholder" versions of forbidden capabilities. See **Q4**.

---

## 1. Objective and success criterion

### Objective

Determine, prospectively and from recorded evidence only, **whether a
trustworthy `ExecutionQuote` for a Solana memecoin entry or exit can be built
from quantities observable without submitting a transaction** — and, where it
cannot, record the honest typed non-observation rather than a plausible number.

This is a **decision milestone, not a capability milestone**. "The lane works"
is not the same claim as "we can quote". A run that concludes
`execution_quote_not_trustworthy` with evidence is a *success* of this milestone
and a *block* on the paper-P&L milestone. That asymmetry is the point.

### Falsifiable success criteria

Each is stated with what would falsify it.

| id | criterion | falsified by |
|---|---|---|
| **SC-1** | **No failure is ever recorded as a success.** Every route observation carries a typed `route_state`; for every row in a success state, that pass's provider ledger shows a successful DexScreener request for that exact token. | one row in a success state whose token had a `failed` / `skipped_cap` / `skipped_budget` entry in that pass's ledger |
| **SC-2** | **Zero incremental provider spend.** Over the activation window the lane's `external_calls` equals the count the same lane makes with the flag OFF, and `solana_tracker_calls == 0`. | any non-zero delta; any non-DexScreener request in the ledger |
| **SC-3** | **No estimate is emitted without its inputs.** `inputs_complete = false` implies every `est_*` column is NULL and `inputs_missing` names what was absent. The rate of `inputs_complete=false` is reported, never hidden. | one row with a non-NULL `est_*` and `inputs_complete = false`, or a non-NULL `est_*` with a NULL `model_id` |
| **SC-4** | **The features discriminate.** Over the window, `pools_exact_base_match` and `top_pool_tvl_share` are non-degenerate across the observed population. | ≥95% of observed tokens landing in one bucket on both — which would mean route composition carries no execution information and the lane should be retired, not extended |
| **SC-5** | **The verdict is reachable in all three directions.** The end-of-window report emits exactly one of `route_composition_usable` / `impact_estimate_bounded` / `execution_quote_not_trustworthy`, and the third is a defined, reportable, non-error outcome. | a report that cannot express "not trustworthy", or one whose verdict is not derivable from the recorded rows alone |

SC-1, SC-2 and SC-3 make the lane *trustworthy*. SC-4 and SC-5 make it *worth
having*. A lane that passes SC-1..SC-3 and fails SC-4 should be deleted, and
that is an acceptable outcome of this milestone.

---

## 2. Affected files and surfaces

Nothing in this list is touched by this document. This is the surface an
*implementation* milestone would cross.

**Read and reused, unchanged:**

- `app/services/crypto_sparse_observation.py` — the pattern and the host. The
  two-phase split (`_fetch_phase`, `:830-998`, which has **no `session`
  parameter** and therefore structurally cannot write), the typed status
  vocabulary (`:232-257`), terminality-by-cause (`:1929-1970`), the single
  terminal funnel `_finish` (`:512`), and the exact-token identity gate
  `_identity_matched` (`:792-827`).
- `app/services/crypto_horizon.py` — `select_pair` (`:446`), `describe_pair`
  (`:492`), `pair_is_eligible` (`:411`), `liquidity_field_state` (`:392`),
  `recent_txns` (`:382`), and the `OBS_*` status constants (`:128-143`).
- `app/adapters/dexscreener.py` — **unchanged, deliberately.** The shared
  adapter serves the scout / meme / discovery / frozen-horizon lanes; the sparse
  lane's own docstring explains why a hard identity gate belongs in the lane and
  not in the adapter (`crypto_sparse_observation.py:806-812`). The same
  reasoning applies here.
- `app/services/crypto_provider_policy.py` — the run-scoped deny set (`Provider`
  enum at `:28-36`, `PAID_PROVIDERS` at `:39`).
- `app/telemetry/` — the shared append-only JSONL sink. **No new telemetry store
  and no telemetry table.** See §5.4.
- `app/services/crypto_tape.py:124` — `LAUNCHPAD_DEXES`, for dex-family
  classification.

**Would be added / modified by an implementation milestone:**

- `app/services/solana_route_observation.py` (new) — pure derivation plus the
  typed vocabulary.
- `app/models.py` — one new table (§5).
- `alembic/versions/0030_*.py` (new) — one additive table, up/down tested.
- `app/config.py` — one default-OFF flag plus its bounds.
- `app/cli.py` — one report command; **no run command that can enable itself.**
- `docs/FEATURE_FLAGS.md`, `docs/SAFETY_BOUNDARIES.md`,
  `docs/CAPABILITY_MATRIX.md` — a new boundary bullet. **This is an explicit,
  non-silent change to a safety document and must be reviewed as one.**
- `tests/test_solana_route_observation_001.py` (new).

**Explicitly NOT touched:** `infra/systemd/**`, `docs/EVO_X2_RUNBOOK.md`, the
frozen-cohort lane, `DexScreenerAdapter`, any MarketOps behaviour, any retention
window, any pragma, and any existing migration.

---

## 3. What is being observed, and what cannot be

This section is the heart of the document.

### 3.1 The quantities an `ExecutionQuote` actually needs

To make a later `ExecutionQuote` trustworthy for a memecoin entry you need, at
the quote instant:

1. the reference price,
2. the **depth** available at that price,
3. the **price impact** of a print of a given notional against that depth,
4. the **route** that print would take — which pools, in what proportion, in how
   many hops,
5. the **fees**: pool fee per hop, Solana base fee, priority fee, associated
   token account rent if the account does not exist, and any token-2022 transfer
   fee,
6. the **realized slippage** between quote and fill,
7. the **landing probability** and slot delay,
8. the **adversarial cost** — sandwich / MEV extraction between quote and
   landing.

### 3.2 The split — observable, estimated, or impossible

| # | quantity | status | source | error source / why |
|---|---|---|---|---|
| 1 | mid price (`price_usd`) | **OBSERVABLE** | DexScreener, already fetched | provider-derived, per-pool; staleness bounded by the pass |
| 2a | pool TVL in USD (`liquidity.usd`) | **OBSERVABLE** | `dexscreener.py:104` | it is a *provider-computed USD aggregate*, not reserves. Depth-shaped, not depth. |
| 2b | pool **reserves** (base/quote token amounts) | **UNVERIFIED** | `liquidity.base` / `liquidity.quote` are **not parsed** (`dexscreener.py:104` reads only `usd`) and appear in **no fixture in this repo** | if absent, a constant-product model has no honest inputs. **This is the CP-0 fork.** |
| 3 | **price impact at a given notional** | **ESTIMATED ONLY** | derived, and only if 2b exists | §3.3 — the error is *unbounded* for concentrated-liquidity pools |
| 4 | **route composition** (pools, split, hops) | **NOT OBSERVABLE from DexScreener.** Observable only from an aggregator quote (Q1) | proxy available: the *pool inventory* the sparse lane already receives and currently discards | a pool inventory says what routes *could* exist, never which one a router would choose |
| 5a | pool fee per hop | **NOT OBSERVABLE** from the payload | a per-`dexId` constant table (assumption to verify at CP-0) | a dexId absent from that table must yield `fee_bps_known=false`, never a default |
| 5b | Solana base fee + priority fee | **OBSERVABLE via RPC** (`getRecentPrioritizationFees`) — a *new provider*, §4 option B | not available from any provider currently in this repo |
| 5c | associated-token-account rent | protocol constant (assumption to verify) | — | one-time per (wallet, mint); moot without a wallet, real for any future quote |
| 5d | token-2022 transfer fee / transfer hooks | **NOT OBSERVABLE** from DexScreener; needs a mint-account decode (RPC) or a token-security provider | if present and unaccounted, **every quote is wrong by an unknown multiplicative factor** | a silent-wrongness risk, not a noise risk |
| 6 | realized slippage | **NOT OBSERVABLE prospectively.** Retrospectively measurable only from a per-trade feed (paid — §4 option D) or by executing | it is a function of pool state *at the landing slot*, which does not exist until you land |
| 7 | landing probability, slot delay | **NOT OBSERVABLE WITHOUT SUBMITTING A TRANSACTION** | — | no provider sells you your own counterfactual |
| 8 | MEV / sandwich extraction | **NOT OBSERVABLE WITHOUT SUBMITTING A TRANSACTION** | — | it is a response *to your own order*, which does not exist until you send it |
| 9 | exit-side depth at t+Δ | **OBSERVABLE** — as a *later observation* | exactly what the sparse lane already buys at 6h/24h | reuse; no new spend |

### 3.3 Why the impact estimate's error is not merely "large"

If reserves are available (2b), a constant-product model gives a closed-form
impact for a balanced two-asset pool. That model is wrong in four named ways,
and one of them is unbounded:

- **(a) curve family unknown.** `dexId` tells you the venue family — this repo
  already separates launchpads from AMMs (`crypto_tape.py:124`: `pumpfun`,
  `moonshot`, `launchlab`) — but not the curve parameters. A bonding curve and a
  constant-product AMM with the same TVL have materially different impact.
- **(b) concentrated liquidity — the unbounded term.** For a CLMM pool, TVL sums
  value across *all* tick ranges. The depth actually available at the current
  price can be an arbitrarily small fraction of it. A CPMM model applied to a
  CLMM pool's TVL does not have a wide error bar; it has **no valid error bar at
  all**, because the truth ranges from "roughly right" to "off by orders of
  magnitude" with nothing in the payload to say which. The only correct response
  is to refuse to estimate and record why.
- **(c) reserve split assumed.** With TVL-in-USD only, the model must assume a
  50/50 split, which is exactly true only for a balanced CPMM at the observation
  instant.
- **(d) single-pool assumption.** A real router splits across pools, which
  *reduces* impact relative to a single-pool model. That makes the single-pool
  estimate **conservative** — the one error direction that is safe, and worth
  recording explicitly rather than leaving implicit.

### 3.4 The finding, stated plainly

> **Two quantities cannot be observed at all without executing a swap:
> (a) whether the transaction lands, and in which slot, and (b) how much value
> adversarial flow extracts between quote and landing. Both are first-order for
> a memecoin fill, not second-order corrections.**
>
> Therefore **any `PaperFill` this project ever writes is a MODEL OUTPUT, never
> a measurement.** The ledger must carry that distinction structurally — a
> `basis` field valued `modelled` and a mandatory `model_id`, never a bare
> number in a price column. A `RealizedPaperPnL` built on an unlabelled modelled
> fill is a fabricated measurement wearing the clothes of an observed one, which
> is precisely the failure class this repository has spent five milestones
> closing.

> **Second finding.** Route composition — quantity 4, arguably the most
> load-bearing single input to an `ExecutionQuote` — is **not derivable from any
> free source currently in this repository.** Everything this milestone can
> build without a boundary amendment is a *proxy* for it. The proxy may be good
> enough; CP-0 and CP-6 exist to find out. It is not the thing itself, and this
> document will not call it the thing itself.

---

## 4. Provider options

| id | option | what it adds | cost | rate limit | licensing | boundary impact | verdict |
|---|---|---|---|---|---|---|---|
| **A** | **DexScreener pool inventory — the response the sparse lane already receives** | pool count, per-pool TVL / dexId / volume / price, TVL concentration, venue mix | **free, no key** | `token-pairs` documented at **300 rpm** in `dexscreener.py:225`; **this option makes ZERO incremental requests**, so it consumes none of it | public unauthenticated GET, already in use | none | **RECOMMENDED — this milestone** |
| **B** | Solana RPC account reads (pool accounts for true reserves; mint accounts for token-2022 extensions) | quantities 2b and 5d honestly; removes error (c) entirely | public endpoints are free but heavily rate-limited and unsuitable for a scheduled lane. Paid RPC has a real monthly cost — **I have not verified any provider's current pricing and will not quote a figure** | provider-dependent, **unverified** | provider-dependent | **none, provided `simulateTransaction` is never called.** An account read is a read. | **NEEDS ERIC (Q2)** — also the largest engineering item here: per-DEX account-layout decoding |
| **C** | DEX aggregator quote endpoint | quantities **3 and 4 directly** — routed output amount, price impact, pool splits. The *only* option that observes route composition rather than proxying it | a free tier exists; hosted-tier pricing **unverified** | rate-limited, **unverified** | unauthenticated GET, no wallet, no signature, no submission | **BLOCKED.** `docs/SAFETY_BOUNDARIES.md:19` plus the `jupiter`/`swap` identifier ban (`frontier_eval.py:62-63`). Requires an explicit boundary amendment | **TIER 3 — ERIC ONLY (Q1)** |
| **D** | Birdeye / SolanaTracker per-trade feeds | quantity 6 (realized slippage) **retrospectively** — the only route to validating any model against something resembling ground truth | **PAID.** Both are in `PAID_PROVIDERS` (`crypto_provider_policy.py:39`) and require per-provider confirmation; generic `--yes` never authorizes one | provider-dependent | keyed | none | **NOT IN THIS MILESTONE.** SolanaTracker spend is excluded by instruction. See **Q6** for a bounded validation-only exception |
| **E** | GoPlus token security (already integrated — `crypto_risk_engine.py`) | *possibly* token-2022 transfer-fee / honeypot state (quantity 5d) | free tier already in use | already accounted | already integrated | none | **CHECK ONLY at CP-0** — a one-line "does it carry this field" question, not a new dependency |

### Recommendation

**Build option A, and only option A, in this milestone.** It is free, it makes
zero incremental provider requests, it moves no boundary, it needs no new
adapter, and it is *already being thrown away*: the sparse lane fetches every
pool for a token and keeps one (`AUDIT_CANDIDATE_LIMIT = 0`,
`crypto_sparse_observation.py:229`). Route-composition proxies are information
this project has already paid for and currently discards.

Then escalate B and C as decisions, not as work. Do not touch D.

The honest framing of that recommendation: **option A alone cannot produce a
trustworthy price impact.** It can produce a trustworthy *depth and venue
composition* record, plus a declared, versioned, conservative impact estimate
that is NULL whenever its inputs are absent. If Eric wants a real
`ExecutionQuote` number rather than a bounded proxy, the answer is Q1 or Q2, and
it costs either a boundary amendment or money. Saying that plainly is more
useful than shipping a number that looks like an answer.

---

## 5. Data model sketch

### 5.1 What was rejected, and why

**Rejected: writing route composition into
`crypto_horizon_observations.raw_payload`.** It needs no migration, which is why
it is tempting. It is wrong. RAW-PAYLOAD-STORAGE-001 measured raw payloads at
27% of the production database with zero readers and cut ticks 2051 B → 118 B;
the sparse lane then measured that keeping three per-candidate diagnostics cost
**424 MB/year of a ~750 MB/year lane — 71% of its growth — on a 4.55 GB database
already past a 3,072 MB gate** (`crypto_sparse_observation.py:219-229`;
thresholds at `docs/SAFETY_BOUNDARIES.md:86`), and set
`AUDIT_CANDIDATE_LIMIT = 0` for exactly that reason. Re-inflating `raw_payload`
is the mistake this repository has just finished undoing.

**Rejected: one row per (observation, pool).** At the measured arrival rate
(`crypto_sparse_observation.py:210-214`: 24h births 517.0, planning rate ~530)
and two horizons per birth, that is ~1,060 observations/day; at ~4 pools each,
~1.5M rows/year. On a database past its own growth gate that is not justified
before the summary has been shown to be insufficient.

### 5.2 Proposed: one narrow summary table, no raw payload

One row per horizon observation, keyed to the observation it derives from.
~1,060 rows/day, ~390k rows/year, all typed scalars.

**Table `crypto_route_observations`** — the name deliberately avoids
`swap` / `quote` / `order` vocabulary:

| column | type | notes |
|---|---|---|
| `id` | int PK | |
| `observation_id` | FK → `crypto_horizon_observations.id`, **unique** | inherits the identity contract and the (cohort, token, horizon) uniqueness for free |
| `chain`, `token_address`, `horizon` | denormalized for query | |
| `observed_at` | datetime | the *logical* observation time, per `_logical_clock` (`crypto_sparse_observation.py:1676`) |
| `route_state` | string, indexed | the typed vocabulary — §6 |
| `pools_returned` | int | every pool the provider named for this token |
| `pools_exact_base_match` | int | survivors of the Gate-1 identity filter |
| `pools_quote_side_excluded` | int | deliberately excluded depth, made visible — §6 R2 |
| `pools_eligible` | int | `pair_is_eligible` survivors (`crypto_horizon.py:411`) |
| `tvl_total_usd` | float, **nullable** | summed over eligible exact-base pools only; NULL — never 0.0 — when no pool contributes |
| `top_pool_tvl_usd`, `top_pool_tvl_share` | float, nullable | |
| `top_pool_address`, `top_pool_dex_id` | string, nullable | |
| `venue_concentration_hhi` | float, nullable | Herfindahl over pool TVL shares — one number for depth fragmentation |
| `dex_family` | string | `launchpad` / `amm` / `clmm` / `mixed` / `unknown`, from an explicit dexId table seeded from `crypto_tape.py:124`. An unlisted dexId is **`unknown`, never guessed** |
| `volume_1h_usd_total` | float, nullable | route-relevant turnover |
| `model_id` | string, nullable | **non-null whenever any `est_*` is non-null** |
| `inputs_complete` | bool | |
| `inputs_missing` | JSON, bounded fixed-key list | typed reasons only, never free text |
| `est_impact_bps_at_notional_a` / `_b` | float, **nullable** | §5.3 |
| `est_impact_error_class` | string, nullable | `bounded_cpmm` / `curve_unknown` / `reserves_unmodelled` / `clmm_depth_unmodelled` |
| `est_bias_direction` | string, nullable | `conservative` (single-pool assumption, §3.3d) / `unknown` |
| `fee_bps_known` | bool | |
| `fee_bps` | float, nullable | |
| `created_at` | datetime | |

**By construction there is no side, size, dollar-P&L, EV, profit, action,
recommendation, order, wallet, key, signing, swap, or execution column.**

**Migration: `0030`, additive, one table, no data change and no column change to
any existing table, up/down tested** per `docs/TESTING_POLICY.md`. Head is
`0029` (`alembic/versions/0029_horizon_member_cohort_added_at.py`).

### 5.3 The notional columns — the one place this design grazes a boundary

`est_impact_bps_at_notional_a` is parameterized by a dollar amount. **That is
the closest this design comes to the position-sizing boundary and it must not be
waved through.**

The honest framing: *"how far does the price move for a print of size X"* is a
property of the **pool**, in the same way a bid-ask spread is a property of a
**book**. It is a depth probe, not a chosen position size, and the notionals are
fixed constants of the measurement instrument — not derived from any signal, any
conviction, any capital base, or any token-specific input. Nothing reads them to
decide anything.

That framing is defensible. It is also exactly the sort of framing an agent
should not self-certify. It is **Q3**, and the fallback if Eric says no is to
store `tvl_total_usd` and `venue_concentration_hhi` only, and let a later,
separately-approved consumer apply a notional at read time.

### 5.4 Telemetry

**No new telemetry store, no new telemetry table.** The lane reuses the shared
append-only JSONL sink (`app/telemetry/`, SQLITE-LOCK-TELEMETRY-001A).

`WRITER_NAMES` (`app/telemetry/schema.py:66-74`) holds no route-specific name.
Two options, and the choice matters:

- **RECOMMENDED — ride the existing reserved name `crypto_horizon_observe`**,
  already used by the sparse lane (`crypto_sparse_observation.py:500`). Stage A
  computes inside the sparse pass and makes zero additional requests and zero
  additional commits, so it is not a separate writer and should not claim to be
  one. **No change to `WRITER_NAMES`, `RUN_STATUSES`, or `STOP_REASONS`.**
- **Rejected for Stage A — a standalone `solana_route_observe` writer name.** It
  would require widening three closed label sets, which is a contract change to
  a bounded-cardinality guarantee (`schema.py:76-91` explains why they are
  closed). Worse, `normalize_run_status` maps an unrecognised status to
  `"other"` (`app/telemetry/writer_pass.py:144`), so a new lane emitting new
  statuses would have them silently collapse. Not worth it for a lane that
  shares a pass.

---

## 6. The identity and fabrication contract

### 6.1 Inherited closures

Stage A derives **entirely from the same provider response the sparse lane has
already validated**, so it inherits all five closures that lane established —
**but only if it derives from the identity-gated list `mine`
(`crypto_sparse_observation.py:939`) and never from the raw `pairs`.** That is
the single most important implementation constraint in this document.

| inherited fabrication shape | typed non-observation |
|---|---|
| transport failure (429 / timeout / 5xx / undecodable JSON), detected by the ledger delta `_transport_failures` (`:775-789`) | `request_failed` — and **no route row is written at all** |
| deterministic cap / budget skip — no request was made | `request_failed` (same ledger delta) — no route row |
| non-list 200 (the `expect=` shape check, `dexscreener.py:200-206`) | `request_failed` — no route row |
| non-empty list parsing to zero usable pairs (`dexscreener.py:247-254`) | `request_failed` — no route row |
| wrong-token identity (`_identity_matched`, `:792-827`) | `identity_mismatch` — no route numbers, ever |

### 6.2 New fabrication shapes specific to route observation

| id | fabrication shape | why it is tempting | typed non-observation / closure |
|---|---|---|---|
| **R1** | **Aggregating TVL over foreign pools.** `pairs` contains pools whose base token is not T; summing their TVL invents depth this token does not have. | it is the natural one-liner: `sum(p.liquidity_usd for p in pairs)` | every route statistic is computed over `mine` only. If `mine` is empty and the answer was non-empty, `route_state = identity_mismatch` and **no numeric column is written**. This is the rule Gate 1 already enforces for the price tick, extended to the aggregate. |
| **R2** | **Quote-side inclusion.** A pool where T is the *quote* token does hold tradable depth for T, so excluding it under-counts. | it is genuinely real depth | **EXCLUDE**, consistent with `_identity_matched`'s reasoning (`:814-819`): that pool's `liquidity_usd` and `price_usd` describe the *base* asset, so including it mixes units. Record `pools_quote_side_excluded` so the under-count is **visible rather than invisible**, and set `est_bias_direction` accordingly. A stated conservative bias is acceptable; a silent one is not. |
| **R3** | **TVL absent / null / zero / malformed rendered as `0.0`.** | a float column wants a float | `liquidity_field_state` already types this (`crypto_horizon.py:392-408`). A non-`present` pool counts toward `pools_returned` but contributes **nothing** to `tvl_total_usd`. If that leaves zero contributors, `route_state = no_liquidity_state` (reusing `OBS_NO_LIQUIDITY_STATE`, `crypto_horizon.py:131`) and `tvl_total_usd` is **NULL, not 0.0**. Zero is an affirmative claim about depth; missing is not. |
| **R4** | **Estimating impact with missing inputs.** The single largest fabrication risk in this design. | a NULL column looks like a bug; a number looks like a result | `inputs_complete = false` ⇒ **every `est_*` column is NULL** and `inputs_missing` names each absent input. **There is no default fee, no assumed curve, no fallback impact, and no "reasonable" placeholder.** For `dex_family = clmm`, `est_impact_error_class = clmm_depth_unmodelled` and the estimate is NULL by rule (§3.3b) — refusing to estimate is correct behaviour, not a gap. |
| **R5** | **Silent model drift.** The impact model changes; old and new rows are indistinguishable. | models get "improved" | `model_id` is non-null on every row carrying any `est_*` value, and the model constants are pinned by an import-time `raise` plus a test, following the band/cadence invariant pattern (`crypto_sparse_observation.py:183-202` — `raise`, not `assert`, because `python -O` strips asserts). |
| **R6** | **Stale-pool substitution.** Using the previous pass's inventory when this pass got no answer. | the data is right there | forbidden by the same rule as the parent lane: no backfill, no interpolation, no nearest-tick substitution. A pass with no answer writes **no route row**. The observation row already records the failure; a missing route row is the honest signal. |
| **R7** | **Truncation presented as completeness.** A token returns 40 pools; statistics computed over the first 8. | bounded work | Stage A stores **no per-pool detail**, so statistics are always over the whole of `mine` and `pools_returned` always records the true count. If per-pool detail is ever added (Stage B), it needs an explicit `pools_truncated` flag. |
| **R8** | **A route number outliving its provenance.** A consumer reads `est_impact_bps_*` and treats it as observed. | it is a number in a column | the `est_` prefix, a mandatory `model_id`, an `est_impact_error_class` on every estimate, and a hard reviewer rule: **no consumer may read any `est_*` column until CP-6 issues a verdict.** Enforced at CP-4 by a reviewer, not by the type system — stated here as a known soft spot. |

### 6.3 The invariant, in one line

**A failure must never be recorded as a successful observation, and an
*estimate* must never be recorded as an *observation* at all.** The second half
is this milestone's addition to the sparse lane's contract, and it is why
`est_*`, `model_id`, `inputs_complete` and `est_impact_error_class` are
mandatory rather than convenient.

---

## 7. Failure modes and non-goals

### 7.1 Non-goals — explicit

- **No swap. No signing. No wallet. No private key. No capital. No transaction
  submission. No transaction construction. No `simulateTransaction`.**
  Read-only observation only.
- **Dark by default**, behind `ENABLE_SOLANA_ROUTE_OBSERVATION`, default
  **false**, following the flag pattern at `app/config.py:513` and
  `docs/FEATURE_FLAGS.md:34`. Off = no read, no write, no compute, no external
  call — a true no-op, verified at CP-5.
- **No historical mass scheduling.** No backfill pass over existing
  observations.
- **No canary cohort.** No arming, no cohort creation, no membership change.
- **No interpolation, no backfill, no stale-nearest-tick substitution.**
- **No SolanaTracker.** Structurally denied by the run-scoped policy
  (`crypto_sparse_observation.py:751-772`), not by convention.
- **No paid provider of any kind**, and no request that consumes a paid budget.
- **No systemd unit, timer, daemon, or scheduled path installed by this
  milestone.**
- **No change to the frozen-cohort lane, to `DexScreenerAdapter`, to MarketOps,
  to any retention window, or to any existing migration.**
- **No EV, side, size, dollar P&L, order, recommendation, or trade direction** —
  no such column exists by construction.
- **No downstream ledger scaffolding.** No `PaperOrder`, `PaperFill`,
  `Position`, or `RealizedPaperPnL` row, table, field, or placeholder;
  `docs/SAFETY_BOUNDARIES.md:84` bans "disabled" versions of forbidden
  capabilities, and that includes helpful ones.

### 7.2 Failure modes, ranked

| rank | failure mode | mitigation |
|---|---|---|
| **F1 — HIGH** | **Concentrated-liquidity depth.** TVL overstates executable depth by an unbounded, unknowable factor (§3.3b). An impact number derived from it is not imprecise; it is meaningless. | `dex_family = clmm` ⇒ `est_*` NULL and `est_impact_error_class = clmm_depth_unmodelled`. CP-0 measures how much of the real token population that covers — if it is the majority, **the impact model should not be built at all** and the milestone stops at composition-only. |
| **F2 — HIGH** | **The estimate gets used.** A plausible number in a column becomes a number in a P&L two milestones later, and its provenance evaporates. | the `est_` prefix, a mandatory `model_id`, `est_impact_error_class`, a CP-6 verdict whose default is "not validated", and the CP-4 boundary reviewer's explicit charge to hunt for consumers. Honest note: this is a **process** mitigation, not a structural one. It is the weakest link in the design. |
| **F3 — MED** | **Database growth** on a database already past its 3,072 MiB gate (`docs/SAFETY_BOUNDARIES.md:86`). | summary-only, typed scalars, no raw payload, ~1,060 rows/day. The per-pool table is explicitly deferred (§5.1). |
| **F4 — MED** | **Write-lock contention.** A separate transaction per route row would double the pass's commit count on a shared SQLite host — the exact shape OPS-013 retired and CRYPTO-COVERAGE-REPAIR-001 spent five review rounds on (`crypto_sparse_observation.py:838-847`). | the route row is written **inside the sparse lane's existing WRITE-phase batch commit**, never in its own transaction and never in the FETCH phase (which structurally has no session). Proven at CP-3 by a transaction-shape test *and* by the writer-pass telemetry's commit counts. |
| **F5 — MED** | **Boundary drift** — someone adds an aggregator "just for the quote field". | the run-scoped policy denies every provider except DexScreener *before a client is constructed* (`:751-772`); a new provider must be added to the `Provider` enum (`crypto_provider_policy.py:28-36`), which is a visible diff; and `jupiter`/`swap` are banned identifier fragments the AST audit catches (`frontier_eval.py:62-63`). |
| **F6 — MED** | **Token-2022 transfer fees silently invalidate every quote** (§3.2 row 5d). | CP-0 checks whether GoPlus already exposes it. If not, `inputs_missing` carries `token_extension_state_unknown` on **every** row, permanently, until Q2 is resolved. An unknown recorded on every row is honest; an unknown omitted is not. |
| **F7 — LOW** | **DexScreener rate limit** (300 rpm, `dexscreener.py:225`) shared with the sparse lane's fetch. | zero incremental requests by design — the main reason Stage A is scoped as it is. |
| **F8 — LOW** | **Quote-side exclusion under-counts depth** (§6 R2). | deliberate, recorded in `pools_quote_side_excluded`, and biased in the safe direction (`est_bias_direction = conservative`). |

---

## 8. Staged plan

Repo pattern: dry-run → focused independent reviews → dark deployment →
prospective activation. Each gate is independently verifiable and each can
terminate the milestone.

**A reversibility tier is assigned per checkpoint at design time:** **1** =
autonomous, **2** = single confirmation, **3** = dual confirmation.

### CP-0 — Evidence checkpoint. **NO CODE.** (Tier 1)

Answer, from real captured DexScreener payloads — captured **locally**, from
public endpoints, at hand-invoked single-token scale, **not on EVO** (which is
unreachable):

1. Does the pair payload carry `liquidity.base` / `liquidity.quote`? *(The fork.
   Currently unverified: the parser reads only `usd`, `dexscreener.py:104`, and
   no fixture in `tests/` contains them.)*
2. Does it carry any fee, fee-tier, or `labels` field usable for §3.2 row 5a?
3. Which `dexId` values actually appear across the live birth population, and
   which are constant-product vs concentrated-liquidity vs bonding-curve?
4. Does GoPlus (already integrated) expose token-2022 transfer-fee or
   transfer-hook state?

**Deliverable:** a fixture file plus a findings section appended to this
document.

**Gate — a real one:** if reserves are absent *and* the dexId population is
majority CLMM/bonding-curve, the free inputs cannot support an honest impact
model. The correct outcome is then **composition-only: the `est_*` columns are
never created**, and the milestone escalates to Q1/Q2 rather than shipping a
number it cannot stand behind. Stopping here is a success.

### CP-1 — Contract and migration, dry-run only (Tier 2 — schema change)

The table (§5.2), migration `0030` with up/down tests, the typed `route_state`
vocabulary, an import-time `raise` on the model invariants, and the flag
`ENABLE_SOLANA_ROUTE_OBSERVATION` default **false**. Nothing is wired to
anything. `--dry-run` computes and prints; persists nothing.

### CP-2 — Pure derivation, no I/O (Tier 1)

`route_summary(pairs_matching_token, token) -> dict`. No session, no network, no
clock. One named test per fabrication shape **R1–R8**, plus the five inherited
closures re-asserted at this lane's boundary — they are inherited *by derivation
from `mine`*, so a test must prove the derivation actually reads `mine`.

### CP-3 — Wire into the sparse lane's WRITE phase (Tier 2)

Assertions, not assurances:

- `external_calls` with the flag ON equals `external_calls` with the flag OFF on
  the same fixture pass (SC-2).
- commit count unchanged; no new transaction opened (F4).
- the FETCH phase still takes no session parameter — structural, not stylistic
  (`crypto_sparse_observation.py:848-856` documents why the test alone is not
  the guarantee).
- flag OFF ⇒ a pass result byte-identical to `main`.

### CP-4 — Three focused, independent reviews (Tier 2). All must PASS.

| reviewer | charge |
|---|---|
| **fabrication / identity** | Can any failure be recorded as a success? Does every derivation read `mine` and never `pairs`? Is any `est_*` reachable with `inputs_complete=false`? Attack R1–R8 adversarially. |
| **storage / write-shape** | Row growth per day against the DB gate; transaction count and lock hold; is anything re-inflating `raw_payload`? |
| **boundary** | The `AGENTS.md:40` safety grep, the AST identifier audit (`frontier-eval-report --include-safety`), and a read of every column name asking "could this be read as a size, an EV, an order, or a recommendation?" Plus: does any consumer read `est_*`? |

### CP-5 — Dark deployment, flag OFF (Tier 2)

**Not schedulable while EVO-X2 is unreachable.** Verify the no-op is a true
no-op: no read, no write, no compute, no external call.

### CP-6 — Bounded prospective activation, then verdict (Tier 3)

Flag ON for a bounded, pre-declared window. Then a report emitting exactly one
verdict:

- `route_composition_usable` — depth/venue composition discriminates and is
  trustworthy; a later `ExecutionQuote` may carry composition fields with
  observed provenance.
- `impact_estimate_bounded` — additionally, the impact estimate's inputs were
  complete often enough and its error class was `bounded_cpmm` often enough to
  be usable **with its stated bias**.
- `execution_quote_not_trustworthy` — **the free inputs do not support a
  trustworthy `ExecutionQuote`.** The paper-P&L milestone is blocked at this
  gate and the next decision is Q1 or Q2.

All three are successful terminations of this milestone.

---

## 9. Validation plan

| checkpoint | proven by |
|---|---|
| CP-0 | captured payload fixtures committed under `tests/`; the four questions answered in writing with the evidence attached; the fork decision recorded in this document |
| CP-1 | `alembic upgrade` / `downgrade` round-trip test; flag-off no-op test; import-time invariant test that deliberately checks the `raise` (since `python -O` strips `assert`) |
| CP-2 | one named unit test per R1–R8; a purity test that the derivation is a function of `(list[PairData], token)` alone; a test that injecting a foreign-base pool into `pairs` changes **no** output column |
| CP-3 | flag-on/flag-off `external_calls` equality; commit-count equality; an assertion that `_fetch_phase`'s signature is unchanged; full suite green |
| CP-4 | three independent PASS verdicts, each recorded; the `AGENTS.md:40` safety grep clean; `frontier-eval-report --include-safety` clean |
| CP-5 | on-host: flag OFF, one pass, result identical to pre-deploy; zero rows in the new table. **Blocked on EVO reachability.** |
| CP-6 | the coverage/trustworthiness report, computed from persisted rows only, emitting one of the three verdicts; SC-1..SC-5 each evaluated explicitly and each reported pass/fail |

**A known gap in this plan.** SC-1 is checkable *after the fact* only if the
per-pass provider ledger is persisted. It currently is not — the sparse lane
reports the ledger in the pass result but does not persist it to a run table
(`docs/FEATURE_FLAGS.md:34` says so explicitly). SC-1 as written therefore
requires either persisting the per-pass ledger or evaluating SC-1 live during
the window from the pass result. That is a real weakness and it is called out
rather than assumed away — see **Q7**.

---

## 10. Open questions for Eric

Ordered by how much they block.

**Q1 — TIER 3, boundary. May a read-only DEX aggregator *quote* GET be
permitted?** This single decision determines whether route composition and price
impact are **observed** or **estimated**. The request carries no wallet, signs
nothing, constructs nothing and submits nothing — but
`docs/SAFETY_BOUNDARIES.md:19` names the surface, and `jupiter`/`swap` are
banned identifiers (`frontier_eval.py:62-63`). Permitting it requires an
explicit amendment in the KALSHI-READONLY-AUTH-001 style
(`docs/SAFETY_BOUNDARIES.md:21-80`), which exists precisely because "quietly
deciding that the old row obviously did not mean this is how a boundary stops
meaning anything" (`:57`). **I will not make this call.**

**Q2 — TIER 3, spend. Paid Solana RPC?** It is the no-amendment route to true
reserves and token-2022 state — the inputs that turn the impact estimate from a
declared model into something modelled from observed reserves. It costs money
(amount **unverified**; I will not quote a figure I have not checked) and it is
the largest engineering item in any version of this milestone (per-DEX account
layout decoding). It moves no boundary.

**Q3 — TIER 2. Is a notional-parameterized depth probe acceptable?**
`est_impact_bps_at_notional_a` names a dollar amount. §5.3 argues it is a pool
property, not a position size. That argument is defensible and is also exactly
the sort of thing an agent should not self-certify. If the answer is no, the
fallback is depth and concentration only, with notionals applied at read time by
a later approved consumer.

**Q4 — TIER 2, sequencing. The target ledger's downstream half is a forbidden
capability today.** `PaperOrder` / `PaperFill` / `RealizedPaperPnL` fall under
"Paper trading / simulation — none exists" (`docs/SAFETY_BOUNDARIES.md:12`),
gated on MVP-005B, and on CRYPTO-003 for the crypto lane (`:19`). Route
observation has **no permitted consumer** until that gate moves. Should
CRYPTO-003 be re-opened first so this work has somewhere to land, or is route
observation deliberately built ahead of its consumer?

**Q5 — TIER 1/2. If CP-0 shows the free inputs cannot support an impact model,**
do we stop at composition-only (my default recommendation), or escalate
immediately to Q1/Q2? CP-0 is designed so that stopping is a clean, unembarrassing
outcome, but the choice is yours.

**Q6 — TIER 3, spend. A bounded, validation-only paid exception?** SolanaTracker
spend is excluded by instruction. I want to name the one thing it would buy that
nothing else can: a small, hard-capped, one-off pull of per-trade fills (order
of magnitude a few hundred requests) would let us measure **realized slippage**
against the model — the only path to validating any impact estimate against
something resembling ground truth. That is real money and a real exception to a
stated constraint, so it is your call, not mine.

**Q7 — TIER 2. Persisting the per-pass provider ledger.** SC-1 is only checkable
retrospectively if the per-pass DexScreener ledger is persisted, and it is not
today (`docs/FEATURE_FLAGS.md:34`). Options: (a) evaluate SC-1 live during the
window from the pass result, no schema change; (b) persist a per-pass run row —
a second table and more growth on a constrained database. I lean (a), but it
makes SC-1 unverifiable after the fact, which is a genuine weakening.

---

## 11. Assumptions to verify — nothing below is established

Collected in one place so none of it is mistaken for a finding.

1. DexScreener pair payloads carry `liquidity.base` / `liquidity.quote`.
   **Unverified.** Not parsed (`dexscreener.py:104`), not present in any fixture
   in `tests/`.
2. DexScreener exposes any usable pool-fee or fee-tier field. **Unverified.**
3. The dexId → curve-family mapping — which venues are CLMM, which are
   bonding-curve. **Unverified beyond `LAUNCHPAD_DEXES` (`crypto_tape.py:124`).**
4. GoPlus exposes token-2022 transfer-fee or transfer-hook state. **Unverified.**
5. Solana base fee, associated-token-account rent, and the priority-fee RPC
   method name. **Not verified in this repository**, and deliberately not quoted
   as figures anywhere above.
6. Any pricing or rate limit for a paid RPC provider or a hosted aggregator
   tier. **Not verified. No figure is quoted.**
7. The DexScreener rate limits cited (300 rpm token-pairs, 60 rpm profiles) come
   from this repository's own docstrings (`dexscreener.py:215`, `:225`), not
   from a check against the provider's currently published limits.
