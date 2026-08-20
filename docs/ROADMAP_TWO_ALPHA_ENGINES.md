# Roadmap — two independent alpha engines

**Decided 2026-08-20**, while `PROD-ACTIVITY-PROFILE-001` was capturing. This
supersedes the ordering implied by the four design specs merged earlier the same
day: those specs are correct and remain unbuilt, and the reason is below.

---

## The decision

Four design specs were written independently and three of them — `ALPHA-FACTORY-001`,
`VOLATILITY-STATE-ENGINE-001`, `RISK-GOVERNOR-001` — **independently concluded
they cannot function without the same missing artifact: a realized-fill
corpus.** That convergence is the strongest signal we have about what to build
next, precisely because it was not coordinated.

So the next phase is **not** more architecture. It is:

1. **finish the Kalshi profile untouched**,
2. **build the execution-ground-truth layer on Solana**,
3. **begin collecting perishable social data**.

### Priority 1 — `REALIZED-FILL-CORPUS-001`

The dependency three systems named for themselves. Without realized execution
labels, every one of them bottoms out in an assumption:

| spec | what it cannot do without fills |
|---|---|
| `ALPHA-FACTORY-001` | distinguish theoretical from executable edge — its economic gate has no machine-checkable source |
| `VOLATILITY-STATE-ENGINE-001` | compute R4 toxic flow at all |
| `RISK-GOVERNOR-001` | express any constraint in units it can obtain |

**We do not need a commercial trade feed to get ground truth on our own fills.**
A confirmed Solana transaction carries fees and pre/post SOL and SPL token
balances, so the realized amounts are derivable from balance deltas. The
progression is:

> quote observation → tx signature → `getTransaction` → pre/post balance delta
> → realized fill → forward markout

and it is proven against **already-existing historical transactions** before any
capital is involved. The two quantities the corpus exists to produce:

> `ε_fill = C_realized − Ĉ_quote`   and   `AS_h = P_{t+h} − P_fill`

**Populating `ε_fill` with live data eventually requires tiny calibration
trades. Those are NOT authorized and no code path may be able to execute one.**

### Priority 2 — `SOCIAL-TAPE-001` (collection only, no model)

Social data is perishable in exactly the way an order book is. Prices can be
reconstructed six months later; **delivery timing cannot**. Unrecoverable if not
captured now: when our system first saw a post, API delivery delay, account
propagation ordering, deleted posts, message timing, when a contract address
first entered the monitored network.

Hence `source_created_at` vs `our_received_at` is the central schema decision,
not a detail — see doctrine 8, and `SOCIAL-TAPE-001` §2.

**Scope discipline:** build the tape, not the signal. The source universe is
**100–300 named sources whose incremental value is being measured**, never
"ingest Crypto Twitter" — post reads are individually priced against a monthly
cap, so an unbounded rule set is a cost incident, and the collector fails closed
without an explicit budget.

**A note on expectations that should be recorded before any experiment:** a
filtered stream with ~6–7 s P99 delivery latency is very likely **too slow for
sub-5-second alpha** during a fast launch. It may still be informative at 30 s /
2 m / 5 m / 15 m, or for narrative propagation ahead of later retail waves.
**Measure it; do not assume it either way.**

### Downgraded — semantic/structural prediction-market arbitrage

Still worth building as a **scanner**, because the "LLM supplies semantics,
deterministic solver decides economics" architecture is right (doctrine 12). But
it drops below the three lanes above, on our own evidence:

* the headline `$1.12M` result is dominated by **converter-enabled** mechanisms
  (`$1.086M`); the hold-to-settlement component — the only route Kalshi permits —
  was **~$32K**;
* our own worked complement dies before capital is even considered:
  **3.00c gross vs 3.07c fees**;
* violations **concentrate on the side that cannot be executed**.

> A logical violation that cannot be monetized is a **research observation, not
> alpha** (doctrine 11).

## The two engines

```
PREDICTION MARKETS                         MEMECOINS / SOLANA
  PROD-ACTIVITY-PROFILE-001                  REALIZED-FILL-CORPUS-001
        ↓ A capacity                                  +
        ↓ B wire-active panel                    SOCIAL-TAPE-001
        ↓ C prereg amendment check                    ↓
  MARKET-MICROSTRUCTURE-EDGE-001              SOCIAL-LEAD-LAG-001
        ↓                                             ↓
     M0 vs M1 verdict                    on-chain M0  vs  on-chain+social M1
```

Two independent falsification engines. **If both fail we have learned something
substantial.** If either survives its cost floor, the Volatility Engine, Risk
Governor and Alpha Factory finally acquire a reason to become code instead of
documents.

## Standing prohibitions while the six profile windows run

Do not: inspect intermediate per-market profile activity; modify the Kalshi
universe; tune `MARKET-MICROSTRUCTURE-EDGE-001`; change feature thresholds; run
preliminary M0/M1; alter the collector; or add load to EVO during capture
windows.

Window A's 15.7 f/s against P4's 140 f/s is **operationally** interesting and
**must not be interpreted yet** — distinguishing time-of-day from universe
composition is exactly what the six-window design exists to answer, and reading
it now would answer it with one draw.
