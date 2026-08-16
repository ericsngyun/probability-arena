# KALSHI-REPLAY-GENERATION-CONSISTENCY-001

**Status: AUTHORIZED, NOT STARTED.** Scheduled deliberately: **after** the
CP6–CP9 qualification capture, **before** `MARKET-MICROSTRUCTURE-EDGE-001`
consumes replayed books.

Authorizes no capital, no orders, no live trading behaviour.

---

## The invariant

> **Within generation `g`, a market is not publishable until that market itself
> has received its snapshot for `g`.**

---

## The defect

On a multi-market subscription, a book whose own snapshot has **not** yet
arrived in a new generation is still reported publishable — carrying its
**pre-reconnect ladders** — as soon as *any* market re-snapshots. Today's
reconnect path halts only the market named in the rewind-refusing snapshot.

Pre-existing; **not** introduced by KALSHI-TAPE-GENERATION. That change made
reconnects legible in the tape; it did not change `publishable_books()`
semantics, which was correctly left alone rather than altered on an agent's own
authority.

## Why it does not block CP6–CP9

`publishable_books()` has exactly **one caller** — `archive.py:1081`, in the
`replay()` path — and **the collector does not gate archiving on it**. Every
frame is archived regardless of book state, carrying raw + normalized +
`subscription_generation`.

So **the durable tape is unaffected**, and the defect is confined to the
reconstruction view. Because the tape is complete, this is repairable *after the
fact without re-collection* — which is exactly the property `book.py:503-518`'s
raw/normalized retention was built to guarantee. The acquisition opportunity is
not lost by deferring the fix.

## Why it is a HARD PREREQUISITE for microstructure work

Stale pre-reconnect ladders would silently contaminate **OFI, depth, imbalance,
microprice, and liquidity-state labels** — computed from a book that looks
current and is not. Silent, plausible, and wrong is the worst failure mode a
feature pipeline can have, and it is the one this project has already been
bitten by (see the `brier_skill_vs_base_rate` misreading and the two
permanently-null archive columns).

`MARKET-MICROSTRUCTURE-EDGE-001` must not consume replayed books until this
lands.

## Scope

- Make publishability **per-market and generation-aware**, per the invariant.
- A market awaiting its own snapshot for `g` is **not publishable** — that is a
  typed, legible state, not an error.
- Do not weaken the durability or ordering contract; do not alter what is
  archived.
- Anti-vacuity: a test must fail if the invariant is removed.

---

## Related deferred items (recorded, not blocking)

**Cross-session generation — NO ACTION REQUIRED.** `subscription_generation` is
monotonic *within* a capture session, which is sufficient. The proper composite
identity is **`(session_id, subscription_generation)`**. There is no reason to
persist an ever-increasing global epoch merely to make generation numbers unique
across runs — and reading a prior epoch back off the tape would be exactly the
after-the-fact inference the design rules out.

**Replay error-frame sequence handling — DEBT, does not block CP6.**
`replay()` skips non-orderbook frames before dispatch, so it never advances
`seq` for the `error` frames that CP3's fixtures prove consume one. **The live
path handles this correctly**; only replay does not. Raw frames are retained, so
it has the same repairable-without-re-collection property. It becomes blocking
only if sequence conservation over those frame types is one of CP9's explicit
replay assertions.
