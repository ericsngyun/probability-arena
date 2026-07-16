# CRYPTO-HORIZON-CANDIDATE-READINESS-001 — candidate readiness evaluator

Measurement-only. Detects the rare moment when the already-persisted local database
holds an exact two-token candidate set an operator could — **by hand, under explicit
authorization** — turn into a shared-pass horizon canary (CANARY-004). It is the
operational bridge the SHARED-CANDIDATE-FEASIBILITY-001 findings recommended: it
removes the "timing luck / repeated scan" dependency for the moments the current
pipeline already produces, and its accumulated data will show whether the current
discovery cadence actually catches them.

**Read-only and boundary-safe.** Zero provider calls, zero writes from the report
path, no discovery, no cohort/observation creation, no arming, no unit install, no
EV/side/size/order/recommendation/wallet/swap/execution. It **composes** the
deployed feasibility calculations (`_completeness_reason`, `fifteen_window`,
`pair_feasibility`, `safe_arm_deadline`, `ACTIVATION_GRACE`) so the completeness and
shared-window rules cannot drift into a second implementation.

Language is deliberately operational: *operational canary readiness*, *manual review
required*. Never *trade / buy / opportunity / edge / expected value*.

## Readiness states

| State | Meaning |
|---|---|
| `no_complete_candidates` | fewer than two complete-state tokens exist in scope |
| `no_overlapping_pair` | complete candidates exist but none share a 15m window (within the 15-min neighborhood) |
| `pair_detected_not_due` | an overlapping grace-fit pair exists but its shared 15m window opens more than the operator margin in the future — detected, not yet actionable |
| `pair_ready_for_manual_preparation` | shared 15m window opens within the operator margin (or is open) and enough time remains to run the dry-run, create, enter shared `due_now`, and arm |
| `shared_due_now_ready` | both tokens are currently `due_now`, grace fits, and the minimum operational margin still remains to create + arm safely |
| `insufficient_arm_slack` | an overlapping pair exists but the 45s grace + operator margin no longer fits the remaining shared interval |
| `expired` | the pair's shared 15m window has already closed (overlapped only historically) |

A pair is never called ready merely because its windows overlap historically — the
`expired` and `insufficient_arm_slack` states exist precisely to prevent that.

### Classification (per pair)

Given the shared 15m intersection `[open, close]`, evaluation time `now`, the
deployed `ACTIVATION_GRACE` (45s), and the operator margin `M`:

- `deadline = close − grace − M` (latest instant to *begin* preparing and still arm safely)
- grace/eligibility must fit (`shared_pass_eligible`: all four horizon shared windows nonempty **and** grace fits) else `insufficient_arm_slack`
- `deadline < open` → `insufficient_arm_slack` (window too narrow for grace + margin)
- `now > close` → `expired`
- `now > deadline` → `insufficient_arm_slack`
- `now < open − M` → `pair_detected_not_due`
- `now < open` → `pair_ready_for_manual_preparation`
- otherwise (`open ≤ now ≤ deadline`) → `shared_due_now_ready`

## Operational safety margin

The evaluator uses the deployed **45-second activation grace** plus a named internal
measurement constant:

```
OPERATOR_PREP_MARGIN_SECONDS = 180.0
```

The canary procedures encode no operator-margin constant, so this is defined and
documented here (not an environment flag). It covers the five manual canary steps
under human-in-the-loop review — explicit-selection dry-run, atomic cohort creation,
orchestrator dry-run, confirmed arming, post-install verification. Override
per-evaluation with `--minimum-arm-margin-seconds`. It never relaxes completeness.

## Deterministic pair ordering

The highest-priority pair is chosen by **operational criteria only** — never price,
volume, momentum, or EV:

1. greatest remaining safe shared-window slack (`deadline − now`)
2. earliest shared-window close
3. canonical token id ascending (stable tie-breaker)

## CLI

```bash
# current readiness (single instant; historical feasibility != current readiness)
crypto-horizon-candidate-readiness-report \
  [--at <ISO-UTC>] [--limit N] [--require-complete] \
  [--minimum-arm-margin-seconds S] [--format text|json]

# aggregate over accumulated readiness evaluations
crypto-horizon-candidate-readiness-history-report [--limit N] [--format text|json]
```

Both commands: `external_calls=0`, `persisted=false`, zero DB writes, create no
cohort/observation/unit. `--require-complete` is always applied to the live verdict
and cannot be relaxed. When a pair is ready the report prints **operator-review
proposal** commands (the `--dry-run` selection only) — clearly labelled, never a
`--confirm` creation or arming command, and never executed.

## MarketOps measurement hook (default OFF)

A single, isolated, non-blocking, report-only hook runs **after** the crypto
persistence stage inside the existing MarketOps cadence (no new timer, no daemon, no
second scan). It is gated by `marketops_include_candidate_readiness` (default
`false`).

- **Off (default):** the hook is a complete no-op — deploying the code changes no
  MarketOps behavior and writes no readiness data.
- **On:** each cycle it evaluates readiness from persisted data (zero provider
  calls), records a compact summary under `run.summary["candidate_readiness"]`, and
  appends one line to the append-only audit.

The hook **cannot fail the cycle**: any exception is caught (never re-raised, even
under `fail_fast`), recorded in `run.summary["candidate_readiness_error"]`, and
swallowed. It cannot change stage eligibility, provider behavior, the cycle result,
or the exit code.

### Append-only audit

`~/crypto-horizon-readiness/readiness.jsonl` (runtime path, never committed). One
secret-free JSON line per cycle: `run_id`, `marketops_cycle_id`, `evaluated_at_utc`,
`state`, candidate ids, shared-window metrics, `rejection_reason`, `external_calls:0`.
No provider payloads, no token-derived shell strings, no secrets.

## History report

`build_readiness_history_report` groups consecutive same-pair ready cycles into
**moments**, and reports history coverage, count by state, distinct moments/pairs,
ready moments by day, per-moment duration + max slack, median cycle gap, and a
clearly-labelled **estimate** of moments that may have fallen between cycles
(single-cycle moments vs the observed cadence). No market-performance output. This is
what determines whether the current discovery source is operationally sufficient
despite its staleness.

## Activation

Deploying the code does **not** activate live persistence — the hook is off by
default. Activation = flipping `marketops_include_candidate_readiness` to `true`
(explicit human gate), which requires no `.env`/flag change to remain safe and can be
reverted at any time. The report and history CLIs are always available (read-only).
