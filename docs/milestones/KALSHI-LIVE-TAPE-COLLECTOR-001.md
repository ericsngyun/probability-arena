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

### In scope

- A real websocket `Transport` implementation satisfying `app/realtime/kalshi.py:366`,
  using the already-vendored `websockets>=12.0` (`requirements.txt:9`; precedent:
  `app/services/tennis_livefeed.py:135-138`).
- Credential wiring: the one permitted path from `KALSHI_OBSERVER_API_KEY_ID` /
  `KALSHI_OBSERVER_CREDENTIAL_PATH` to `ReadOnlyRequestSigner.from_path`
  (`app/realtime/auth.py:402`) and `headers_for(purpose=WEBSOCKET_HANDSHAKE)`
  (`app/realtime/auth.py:510`). **Today nothing in `app/` reads those variables at all.**
- A collector session loop: connect -> handshake -> subscribe -> read -> normalize -> archive,
  bounded by an explicit `--max-seconds` and an explicit market-ticker list.
- Reconnect and subscription-generation handling, driving the EXISTING
  `SubscriptionState.supersede` / `begin_recovery` (`app/realtime/book.py:689-707`).
- A measurement lane (section 7) that is structurally off the archive's critical path.
- A `kalshi-collect-once` CLI, default-off flag, dry-run, and NO systemd unit installed
  by this milestone.

### Non-goals (explicitly deferred)

- **No timer, no daemon, no MarketOps hook, no systemd unit installed.** Per
  `docs/SAFETY_BOUNDARIES.md:72-74`: "No timer, no service, no daemon and no MarketOps
  hook exists for the observer, and installing one is a separate approved milestone
  (KALSHI-REALTIME-OBSERVATION-001B)." This milestone is the collector; scheduling it is
  001B and stays 001B.
- **No SQLite writes.** The collector writes to the archive filesystem tree and to the
  JSONL measurement sink. It opens no database session. This keeps it entirely outside
  the SQLITE-LOCK-TELEMETRY contention story and the backup-coordination lane.
- **No book publication into any consumer.** `SubscriptionRouter` may be run in-process to
  validate sequence integrity, but no book state is exported, persisted to the DB, or
  read by any forecast/signal/MarketOps path.
- **No REST reconciliation loop.** `reconcile_with_rest` (`app/realtime/archive.py:1091`)
  stays a replay-time function.
- **No rotation-default change in this milestone.** Retuning `DEFAULT_MAX_SEGMENT_*` from
  the measurement is a follow-up whose input is this milestone's output. Shipping the
  measurement and the retune together would mean the retune was never validated against
  an independent number.
- **Production environment is not the default and is gated separately** (open question Q1).

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
