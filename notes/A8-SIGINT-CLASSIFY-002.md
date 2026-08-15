# A8-SIGINT-CLASSIFY-002 scratch notes

worktree: /Users/ericyun/code-stuff/probability-arena/.claude/worktrees/agent-a44bfe3e6902cc1da
HEAD at start: 3b513ef609e9925416a6a547abae08910f0a9558
branch: A8-SIGINT-CLASSIFY-002

## Structural reading (before measurement)
- Test: tests/test_kalshi_async_accounting_harness_001.py::TestRealSignalReproducesTheSameClass::test_ctrl_c_causes_zero_identity_violations[sigint]
- 6 trials = seeds 0..5, each an external subprocess `fault_trial.py --target segment
  --n-interrupts 2 --mode sigint --window-s 0.6 --n-submits 20000`, timeout 25s.
- Two assertions in the sigint arm:
  1. `not violations`  (identity: on_disk_records <= written)   <-- separate finding
  2. `published == n_trials` (every trial's close() must succeed) <-- the failing one
- SIGINT DELIVERY IS WALL-CLOCK, NOT SYNCHRONISED: fault_trial.py:169-176,
  `time.sleep(rng.uniform(0.0, window_s))` per interrupt, in a daemon bomber
  thread. Nothing ties the fire time to the submit loop's progress or to close().
- close() happens at fault_trial.py:198 AFTER the submit loop returns. If a bomb
  is still pending when the loop ends, the SIGINT lands in close() (or the join,
  or the readback) -> close_ok False -> published < 6, WITHOUT any corruption.
- Hypothesis H1: failures are bombs landing after the submit loop, i.e. outside
  the region the test claims to be about. Predicts close_error is a
  KeyboardInterrupt and on_disk/written stay consistent.

## MEASUREMENT 1 -- the timing budget (unloaded, this machine)
Bomb schedule is a pure function of (seed, window_s), no clock:
  seed 0 fires at 0.5067, 0.9614 s      seed 3 at 0.1428, 0.4693
  seed 1 fires at 0.0806, 0.5891        seed 4 at 0.1416, 0.2035
  seed 2 fires at 0.5736, 1.1423        seed 5 at 0.3737, 0.8188
Uninterrupted cost of the region under test:
  20,000-submit loop = 2.325 s   (1k @0.185, 5k @0.683, 10k @1.252)
  close()            = 1.294 s
=> unloaded, the LAST bomb (1.142 s) lands comfortably inside the 2.325 s loop
   for every seed. The test's margin is loop_duration - last_fire = ~1.18 s,
   i.e. the test passes only because the loop happens to outlast the bomber.

## MEASUREMENT 2 -- 22 replications of the 6-trial campaign
runs 0-14  unloaded          : PASS 6/6 published, 0 violations   (15/15)
runs 15-19 under 16x CPU load: FAIL 5,5,4,1,4 published /6, 0 violations (5/5)
runs 20-21 load removed      : PASS 6/6, 0 violations
Perfectly load-gated in both directions. Identity violations: 0 in ALL 22 runs
(132 trials), loaded and unloaded alike.

## MEASUREMENT 3 -- THE DECISIVE PROBE (is the canonical record damaged?)
Forced the interrupt into close() by widening the window past the loop
(--n-interrupts 1 --window-s 3.2, 30 seeds; 2 landed in close()).
For EVERY close_ok=False trial, post-mortem of the archive on disk:

  close_error           : "KeyboardInterrupt: "     (bare, no SegmentError)
  state                 : CLOSING                   (not INVALID)
  manifest files        : []                        (nothing published)
  events.jsonl.gz       : 20,000 records
  written (accounting)  : 20,000    on_disk: 20,000  -> identity holds
  verify_chain          : ok=True, reason=None
  ordinals              : 0..19999, 20,000 unique, contiguous
  accounting            : clean=True, admission_holds=True, disposition_holds=True

=> NOT case (a). The archive is bit-perfect and fully chain-verified; the only
   thing that did not happen is publication of the manifest. Clean
   non-publication, zero corruption.

## MEASUREMENT 4 -- WHERE the un-deferred interrupt lands (4 distinct sites)
close()'s deferral (`with self._defer_commit_signals`) covers ONLY the
flush/fh.close/fsync durability region, segment.py:2230-2239. The interrupt
never lands there. All observed landings are in the UN-deferred, READ-ONLY
reconciliation that follows:
  segment.py:2253  on_disk = read_segment_records(self.events_path)
  segment.py:2259  verdict = verify_chain(on_disk, ...)
(and, once, segment.py manifest stage 'manifest_temp_create').
That region re-reads and re-verifies 20,000 already-fsynced records and costs
~1.2 s of the 1.29 s close(). The bytes are durable BEFORE the interrupt can
land; only the verification/publication step is aborted.

## THE WRITTEN CONTRACT (segment.py:2120-2126, close.__doc__)
  "CLOSING -> reconcile -> durability -> manifest publish -> CLOSED.
   The manifest is written LAST and published by atomic rename, so a crash
   anywhere before that leaves a segment with no manifest -- RECOVERABLE AND
   UNCOMMITTED, NEVER FALSELY CLOSED."
The observed failing trials are precisely the contract's own documented
outcome. The test's `assert published == n_trials` asserts something STRICTLY
STRONGER than the contract: that an interrupt during close() must still
publish.

## MEASUREMENT 5 -- CALIBRATION CURVE (idle machine, everything else identical)
Submit loop = 2.325 s. Vary only --window-s:
  window_s=0.6  max_cum_fire=1.14s  published=5/5  IDENTITY_VIOLATIONS=0
  window_s=0.9  max_cum_fire=1.71s  published=6/6  IDENTITY_VIOLATIONS=0
  window_s=1.2  max_cum_fire=2.28s  published=5/6  IDENTITY_VIOLATIONS=0
  window_s=1.5  max_cum_fire=2.86s  published=5/6  IDENTITY_VIOLATIONS=0
  window_s=1.8  max_cum_fire=3.43s  published=4/6  IDENTITY_VIOLATIONS=0
Publication falls off EXACTLY as max_cum_fire crosses the 2.325 s loop
duration. The assertion is a wall-clock race between the bomber's uniform
sleeps and the loop's duration, with ~1.18 s of unearned margin on idle
hardware. Identity violations: 0 at EVERY point on the curve.

## VERDICT
(c) LOAD-SENSITIVE THRESHOLD CALIBRATION, with the individual failing trials
being textbook (b). Definitively NOT (a).
Recommended fix (NOT applied -- task is classification only): make the bomber
fire on submit-loop PROGRESS (a randomly chosen submit index) instead of a
wall-clock sleep, so every interrupt is guaranteed to land inside the region
under test. Strictly stronger than the status quo -- it also closes the
false-negative the script's own comment at fault_trial.py:154-165 warns about.


