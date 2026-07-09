# EDGE-SELECTION-001 — Pre-registration of candidate row-selection policies

**Locked: 2026-07-09T19:00:00Z.** This document freezes, BEFORE any window
that can validate them, the candidate row-selection policies discovered by
EDGE-FILTER-001 and confirmed relevant by TRIGGER-TIMING-001. It is a
**validation protocol only**: nothing here changes live edge-precheck,
forecasts, MarketOps, EDGE-AUTO, promotion, gates, flags, or automation, and
nothing here computes EV, recommends trades, sizes positions, places orders,
or touches wallets/keys/signing/swaps/execution. A `validated_shadow` outcome
is a measurement-protocol status; **MVP-005B remains blocked unless a human
explicitly accepts it**, regardless of any result under this protocol.

## 1. Why pre-registration

EDGE-FILTER-001 searched **18 shadow policies** and reported the best
performers on the same windows used to find them — a selection procedure whose
winners are upward-biased by construction. The risk is not hypothetical; it is
already on record:

- 2026-07-09 ~17:45 UTC (48h, n=263): `require_gap_follows_move_totals_only`
  cleared the shadow MVP bar for the first time (n≥20, toward≥0.55) — the
  filter report printed `blocked: False`.
- 2026-07-09 ~18:45 UTC — hours later, same day: the 48h window (n=285) had it
  back at toward 0.5385 with n=26 (`too_thin`), and the **24h window (n=150)
  showed toward 0.389** — nowhere near the bar. `blocked: True` again.

A policy whose headline number swings that much within hours cannot be judged
on the window that selected it. From this lock forward, the candidates below
are evaluated **only** against the fixed gates in §4–5, and only windows that
start after the lock count as validation.

## 2. Pre-registered policies (FROZEN)

Exactly these, by their existing `edge_filter_shadow` predicate names. No
policy may be added, removed, reweighted, or re-parameterized without a NEW
pre-registration document with a new lock timestamp. Tests enforce the freeze.

| # | policy (predicate name) | prereg alias | role |
|---|---|---|---|
| 1 | `baseline_all_watchlist` | baseline_all_watchlist | baseline |
| 2 | `require_gap_follows_move_totals_only` | require_gap_follows_move_totals_only | candidate (primary) |
| 3 | `require_gap_follows_move_exclude_spreads` | require_gap_follows_move_exclude_spreads | candidate |
| 4 | `gap_follows_move_and_high_liquidity` | gap_follows_move_and_high_liquidity | candidate |
| 5 | `gap_follows_move_and_tight_spread` | gap_follows_move_and_tight_spread | candidate |
| 6 | `total_only` | totals_only | candidate |
| 7 | `exclude_spread_markets` | exclude_spreads | candidate |
| 8 | `spread_only` | spread_only | **negative control** |

Predicate definitions are frozen as implemented in
`app/services/edge_filter_shadow.py` at commit `f611202` (tight spread ≤ 2¢;
high liquidity ≥ 1,000,000¢; follows-move = re-derived gap agrees in sign with
the prior-10m market move). The negative control is EXPECTED to remain adverse
(toward < 0.50 or negative closure); if `spread_only` turns non-adverse on a
future window, that flags a regime shift or methodology problem and all
candidate results in that window are suspect.

## 3. Discovery snapshot (in-sample — recorded for contrast, can validate NOTHING)

Measured 2026-07-09 ~18:45 UTC on EVO-X2 (`f611202`), immediately before lock.
60m horizon; toward / mean closure.

| policy | 24h (n=150) | 48h (n=285) |
|---|---|---|
| baseline_all_watchlist | n=150 · 0.273 / −0.574 | n=285 · 0.285 / −0.330 |
| require_gap_follows_move_totals_only | n=18 · 0.389 / −0.326 | n=26 · 0.539 / +0.423 |
| require_gap_follows_move_exclude_spreads | n=20 · 0.400 / −0.117 | n=37 · 0.459 / +0.256 |
| gap_follows_move_and_high_liquidity | n=8 · 0.625 / +0.767 | n=29 · 0.483 / +0.349 |
| gap_follows_move_and_tight_spread | n=20 · 0.350 / −0.125 | n=52 · 0.404 / +0.242 |
| totals_only (`total_only`) | n=61 · 0.328 / −0.383 | n=109 · 0.380 / −0.111 |
| exclude_spreads (`exclude_spread_markets`) | n=97 · 0.330 / −0.423 | n=177 · 0.335 / −0.217 |
| spread_only (control) | n=53 · 0.170 / −0.849 | n=108 · 0.204 / −0.514 |

Companion context at lock: TRIGGER-TIMING-001 48h shows no timing policy
promising (best cooldown closure −0.20; `wait_until_gap_follows_move` opposes
0.000 but toward only 0.307) — selection, not timing. MarketOps #1393 ok;
frontier `safety_ok=True`.

## 4. Validation windows

| window | definition | can validate? |
|---|---|---|
| discovery | any window at/before the lock (all data above) | **NO — in-sample; discovery only** |
| next 24h | first 24h window starting after the lock | yes (small-n expected; likely `insufficient_sample`) |
| next 48h | first 48h window entirely after the lock (from ~2026-07-11T19:00Z) | yes |
| next MLB slate | slate-bracketed window over the next full MLB game day after the lock | yes |
| World Cup Jul 9–11 | if watchlist rows appear for WC markets in that span **after the lock** | yes (report separately; different domain) |
| rolling 7d | once ≥7 days of post-lock data exist (from ~2026-07-16T19:00Z) | yes — the primary decision window |

A window that straddles the lock is `mixed` and can NOT validate (the report
labels it and says so). The report classifies every run's window as
discovery / validation / mixed from its `--since/--until/--hours` bounds vs
the lock, and counts rows on each side.

## 5. Success gates (ALL must hold for `validated_shadow`)

1. **Sample**: final-horizon n ≥ **75** (hard minimum; **preferred ≥ 150** —
   met-minimum-but-below-preferred is stated in the status reason).
2. **Direction**: 60m moved-toward rate ≥ **0.55**.
3. **Magnitude**: mean 60m gap closure **positive**.
4. **Concentration guard**: max single-ticker share ≤ **34%** AND max
   single-game/series-cluster share ≤ **50%** of final n.
5. **Out-of-sample**: gates 1–4 hold on a **validation** window (entirely
   after the lock) — the discovery window can never validate.
6. **No regression**: MarketOps healthy and frontier `safety_ok=True` over the
   window (checked via the companion `marketops-report` /
   `frontier-eval-report` runs, not inferred by this report).
7. **Clean invalid profile**: invalid rows remain fully explainable in the
   frontier report (no new unexplained invalidation mode appears).

## 6. Failure gates (ANY marks the candidate `failing_gates` for that window)

- 60m toward < **0.50** on a post-lock window (readable n).
- Mean 60m closure **negative** (readable n).
- Sample collapses below readable (final n < 12 → `sample_collapsed`).
- Concentration guard violated (one ticker > 34% or one game > 50%).
- **Discovery-only pattern**: passes on discovery windows but fails the gates
  on successive validation windows → candidate is retired (requires a new
  prereg to resurrect with changed definitions).
- Negative control anomaly: if `spread_only` turns non-adverse, the window is
  flagged and no candidate can be validated on it.

Between pass and fail: `insufficient_sample` (n < 75, not failing) and
`inconclusive_continue_observing` (toward in [0.50, 0.55) or marginal
closure) — both mean keep observing, decide nothing.

## 7. Protocol rules

- **No peeking-based edits**: policies, thresholds, and gates in this document
  are frozen at lock. Any change = new prereg document + new lock; prior
  post-lock data cannot be reused as validation for changed policies.
- **All windows count**: every post-lock window run is part of the record,
  including failures. No dropping unfavorable windows.
- **One report**: `edge-selection-validation-report --hours N [--since ISO]
  [--until ISO]` evaluates exactly the §2 registry against the §5–6 gates and
  labels the window per §4. It persists nothing and changes nothing.
- **Doctrine**: even a candidate that reaches `validated_shadow` on the
  rolling-7d window changes NOTHING by itself. The next step would be a
  separate explicitly-accepted milestone (e.g. MVP-005B design), and **MVP-005B
  remains blocked unless a human explicitly accepts it**.
