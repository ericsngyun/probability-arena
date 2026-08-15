# KALSHI-LIVE-TAPE-COLLECTOR-001 — live venue -> canonical archive bridge

STATUS: DESIGN ONLY. No production code in this branch.

## 1. Objective

Connect an authenticated, read-only Kalshi market-data websocket to the already-reviewed
canonical synchronous archive, and use the first real sessions to REPLACE the guessed
production rate that `segment.py`'s rotation defaults were derived from with a measured one.

The archive exists. The replay/integrity lane exists. The signer exists. **Nothing joins
them to the venue** — `app/realtime/kalshi.py:366` declares a `Transport` interface whose
only implementation is `FixtureTransport` (`app/realtime/kalshi.py:379`), and no file in
`app/` reads `KALSHI_OBSERVER_API_KEY_ID` / `KALSHI_OBSERVER_CREDENTIAL_PATH`. This
milestone builds exactly that bridge and measures it.

## 2. What this unlocks

### New capability

A **live tape** of Kalshi market data: real venue frames, sequence-validated, normalized,
and written into a genesis-anchored, chained, manifest-committed archive that the existing
`kalshi-realtime-replay` lane can reconstruct deterministically. Today the replay lane can
only replay fixtures and a 4-record demo capture.

### The dependency that disappears

From `MEMORY.md`, the Probability lane's chain to a first trustworthy prospective paper P&L is:

    archive -> [MISSING: LIVE TAPE WRITER] -> replay -> quotes -> ... -> paper fill -> P&L

`KALSHI-LIVE-TAPE-COLLECTOR-001` is that missing step, and only that step. When it lands:

1. **"We have no prospectively-recorded Kalshi quote stream" stops being true.** Every
   downstream measurement in the Probability lane — executable entry quote, exit quote,
   spread at decision time — currently has no prospective source. The archive is the
   source; the collector is what puts anything in it.
2. **The archive's deployment/load gate stops being a guess.** `app/realtime/segment.py:200-212`
   derives `DEFAULT_MAX_SEGMENT_RECORDS = 13_000` against "the ~500 events/s assumed peak
   this milestone's performance gate used" and notes "the one real measured Kalshi rate
   (4 records over ~2 minutes, DEMO)". A measured rate turns three shipped constants from
   assumptions into calibrated values — or refutes them.

### What it explicitly does NOT unlock

It does not produce a quote, a fill, a P&L, an opportunity, or an entry. It produces
evidence on disk plus a measurement report. Every step after "replay" in the chain above
remains a separate, separately-accepted milestone.

## 3. Scope and non-goals

TBD

## 4. Capability boundary

TBD

## 5. Survey of what already exists

TBD

## 6. Component design

TBD

## 7. Measurement plan

TBD

## 8. Failure and backpressure handling

TBD

## 9. Implementation checkpoints

TBD

## 10. Validation plan

TBD

## 11. Open questions for Eric

TBD
