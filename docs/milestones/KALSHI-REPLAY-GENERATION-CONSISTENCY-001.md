# KALSHI-REPLAY-GENERATION-CONSISTENCY-001

**Status: DELIVERED on branch `KALSHI-REPLAY-GENERATION-CONSISTENCY`, NOT
MERGED.** Authorized 2026-08-17 after CP7 proved the defect on the live venue;
implemented the same day. Six required proofs green, including the two
anti-vacuity controls.

Authorizes no capital, no orders, no live trading behaviour. Nothing was
deployed; no venue socket was opened by this work.

---

## The invariant

> **Within generation `g`, a market is not publishable until that market itself
> has received its snapshot for `g`.**

---

## The defect (as found)

On a multi-market subscription, a book whose own snapshot had **not** yet
arrived in a new generation was still reported publishable — carrying its
**pre-reconnect ladders** — as soon as *any* market re-snapshotted.

```python
def publishable_books(self) -> dict:                      # before
    healthy = self.subscription.healthy   # SUBSCRIPTION-level, one flag for all
    return {t: (b.publishable and healthy) for t, b in sorted(self.books.items())}
```

`healthy` is one flag for every market on the subscription, and the first
snapshot of a new generation set it for all of them. Meanwhile
`begin_subscription_generation()` deliberately rebased each book *without*
taking it out of publishable state — it cleared `last_seq` and nothing else — so
every un-re-snapshotted book still carried `synced=True` and no integrity
reason from the previous epoch. The conjunction flipped all of them back at
once.

Pre-existing; **not** introduced by KALSHI-TAPE-GENERATION.

**Measured live (CP7, `s2-reconnect`, 2026-08-17).** At both forced generation
boundaries the first snapshot republished **all 60 markets in one step, 59 of
them still carrying pre-reconnect ladders**. Harm was bounded to a ~36 ms
window with zero new-generation deltas landing on a stale ladder — *only*
because the venue happened to send all 60 snapshots before any delta, which is
not a contract we hold.

## The fix

A per-book epoch identity, in `app/realtime/book.py`:

* `OrderBook.based_generation` records the subscription generation whose **own**
  snapshot based this book. `apply_snapshot` sets it; a generation boundary does
  not.
* `based_for_current_generation` is `based_generation == subscription_generation`
  and **is** the invariant — publishability and the delta guard both read it and
  nothing else does, which is what makes the anti-vacuity control a one-line
  revert.
* `publishable` requires it. A sibling's snapshot re-bases the sibling and says
  nothing about anyone else.
* A **new-generation delta landing on an un-re-snapshotted book is refused**,
  counted as `rejected_pre_generation_snapshot`, and — deliberately — **not
  halted**: nothing is broken, and the market recovers on its own snapshot. That
  closes the case CP7 could not rule out, where a delta from epoch *N* applied
  on top of epoch *N−1*'s ladder fabricates a book that existed at no instant.

**Doctrine 10 — the state is typed, not a silent `False`.** `PublicationState`
carries a closed vocabulary plus **both** generation numbers:

| state | meaning |
|---|---|
| `publishable` | based for the current epoch, sequence-clean |
| `book_halted` | integrity is broken — something is **wrong** |
| `awaiting_snapshot_for_generation` | nothing is wrong; this market has not yet been re-snapshotted into the current epoch |
| `subscription_unhealthy` | the subscription itself is awaiting a base |

A reader can therefore tell "not yet based" from "based and empty" and from a
fault — which matters most for the three ladderless markets in the CP7 capture,
where all three produce a book with zero levels and only one of them is an
observation.

`archive.replay()` returns `publication_states` beside `publishable`, so the
distinction survives the replay lane rather than living only in the collector.

**Not touched:** what is archived, the durability/ordering contract,
`app/realtime/kalshi.py`, `fixedpoint.py`. This is a reconstruction fix, exactly
as scoped.

## The six proofs

`tests/test_kalshi_replay_generation_consistency_001.py` — 25 tests. The
order-book frames are the venue's own bytes, lifted verbatim from
`docs/experiments/KALSHI-CP6-CP9-FUNCTIONAL-RUNS/s2-reconnect-session.json`
(the session that measured the defect), with only `seq` re-numbered; a drift
detector re-reads the artifact and fails if that stops being true (doctrine 9).

1. **The invariant at a boundary, per market.** Eleven markets based in
   generation 1; four re-snapshotted in generation 2. The four are publishable,
   the seven are not, and the transition log has **four entries of one change
   each** — never one entry carrying eleven, which is the exact shape CP7
   measured.
2. **Anti-vacuity, two ways.** Forcing `based_for_current_generation` to `True`
   restores the pre-fix semantics exactly: the same assertion helper proof 1
   passes with then raises, the delta lands on the abandoned ladder, and the
   replay lane reproduces the defect. Independently, reverting
   `app/realtime/{book,archive}.py` to the base commit turns **17 tests red**,
   including CP7's own property test.
3. **Cold start unchanged** — every market still acquires on its own snapshot,
   zero faults, zero reconnects.
4. **The fault path unchanged** — a real withheld frame, exactly one recovery,
   and each market re-acquires separately *inside one generation*.
5. **The drop detector is not blinded** — a within-generation gap still raises,
   still unpublishes every book, and is still reported as `book_halted` rather
   than as the benign boundary state. Epoch-less legacy tapes still publish and
   still fault.
6. **Replay agrees with live** at **every order-book record** across the
   boundary, by prefix-replaying the same tape against the live transition log.
   The one genuine lane difference — the live collector supersedes on the
   `subscribed` ack, before any new-generation frame can be read — is measured
   and asserted to be bounded to frames the replay function does not read.

CP7's `test_each_market_regains_publishability_only_on_its_OWN_new_snapshot` was
a `strict` xfail; it now passes and the marker is removed, with the history kept
in its docstring as the anti-vacuity record.

## What this does and does not clear

`MARKET-MICROSTRUCTURE-EDGE-001`'s blocker is removed **for the reconstruction
path**: stale pre-reconnect ladders can no longer be presented as current, in
either lane. It clears nothing else — no rate, latency, capacity or
microstructure-realism claim is affected, and CP7 was re-run only offline.

---

## Related deferred items (recorded, not blocking)

**Cross-session generation — NO ACTION REQUIRED.** `subscription_generation` is
monotonic *within* a capture session, which is sufficient. The proper composite
identity is **`(session_id, subscription_generation)`**.

**Replay error-frame sequence handling — STILL DEBT.** `replay()` skips
non-orderbook frames before dispatch, so it never advances `seq` for the `error`
frames that CP3's fixtures prove consume one. The live path handles this
correctly. Untouched here: it is a different defect, and widening `replay()`
inside this milestone would have meant qualifying something other than what CP7
measured.

**The `recoveries` counter still does not conserve.** `supersede()` counts a
recovery on the live side and the tape has no record of a collector *action*
(CP8 §4.5). Proof 6 asserts that this is the **only** remaining subscription-stat
difference, so it can neither grow silently nor be mistaken for this defect.
