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
Fix APPLIED (classification-only constraint lifted by the coordinator for this
test-harness-only change): the bomber fires on submit-loop PROGRESS (a
randomly chosen submit index drawn from the same seeded rng) instead of a
wall-clock sleep, so every interrupt is guaranteed to land inside the region
under test. Strictly stronger than the status quo -- it also closes the
false-negative the script's own comment at fault_trial.py:154-165 warns about.
See "POST-FIX" below.

## FOLLOW-UP TICKET (SECONDARY FINDING) -- NOT A BUG, NOT FIXED HERE
`close()` spends ~1.2 s of its 1.29 s in UN-DEFERRED, read-only
re-verification (`segment.py:2253` read_segment_records, `:2259`
verify_chain). The deferral at `:2230` covers only flush/`_fh.close()`/fsync.
Consequence: an operator's Ctrl-C during shutdown RELIABLY costs the manifest,
because the un-deferred window is by far the largest part of close().

The behaviour is CORRECT AND FAIL-SAFE and matches `close.__doc__` exactly:
no manifest, uncommitted, never falsely CLOSED, bytes intact and
chain-verified. It is a RECOVERY-ERGONOMICS gap, not a correctness defect:
an in-process `close()` retry does not work --

    first close() interrupted: KeyboardInterrupt   state=CLOSING
    RETRY close() -> SegmentError: event-file durability failed:
        AttributeError("'NoneType' object has no attribute 'flush'")

because `_fh` has already been closed and cleared. Publishing those records
therefore needs out-of-process, `archive-recover-head`-style tooling. Worth
its own ticket; explicitly OUT OF SCOPE here, and NO `app/` change was made.

## POST-FIX PROOF (progress-gated bomber)
Same 6-trial campaign, same test counting semantics, 20 runs each.

UNLOADED  : 20/20 PASS. published==n_trials every run (6/6), hang=0,
            bombs_missed=0 (240/240 bombs landed in the loop),
            IDENTITY_VIOLATIONS=0. Delivery lag 0-51 submit iterations past
            the target -- i.e. the interrupt lands within ~51 of 20,000
            iterations of where it was aimed, against 3,000 iterations of
            runway. Margin is ~59x and denominated in progress, not seconds.

LOADED 16x: (the exact load that failed the OLD form 5/5)
  runs 0,3..19  PASS 6/6 published, hang=0
  run 1         published(2)==n_trials(2), hang=4   <- see residual below
  run 2         published(4)==n_trials(4), hang=2   <- see residual below
  ALL 20 runs: published == n_trials, bombs_missed=0, IDENTITY_VIOLATIONS=0.
  Delivery lag 0-98 iterations. NOT ONE trial failed to publish, loaded.

Before/after on the identical load: old form 5,5,4,1,4 published out of 6 ->
new form 6/6 (or n/n) in all 20. The publication assertion is now load-
insensitive because the schedule is progress-denominated.

pytest: `tests/test_kalshi_async_accounting_harness_001.py` 3 passed 1 skipped
(29.6s); with KALSHI_ASYNC_ACCOUNTING_CAMPAIGN=1, 4 passed (413s) -- the
24-trial, 3-interrupt campaign included.

## RESIDUAL LIMITATION (reported, not hidden)
Under 16x CPU load the 25 s `subprocess.run` timeout still expires for some
trials (runs 1 and 2 above: 4 and 2 hangs). This is PRE-EXISTING, unrelated to
the bomber change -- a 20,000-submit trial simply takes longer than 25 s of
wall clock when the machine is 2x oversubscribed -- and the test already
classifies it separately via `assert n_trials >= REAL_FAULT_TRIALS // 2`,
whose own message calls it "timing mis-calibration, not a finding". Run 1's
2 usable trials would trip that guard. So the PUBLICATION assertion is now
load-insensitive, but the campaign can still be starved of usable trials on a
badly oversubscribed machine. Fixing that means raising the subprocess timeout
or shrinking --n-submits; it is a different knob and was deliberately NOT
touched here.

Also residual by construction: delivery is at the main thread's next
bytecode-boundary check after `os.kill` (plus, inside `submit()`'s deferred
commitment region, at that region's exit), so an interrupt lands NEAR its
target index, not exactly on it. Measured 0-98 iterations against 3,000 of
runway, and `bombs_missed` makes any escape from the region a loud failure
rather than a silent pass.

Related wording nit, also not fixed: the assertion messages in the test say a
non-publishing trial leaves records "unpublishable". That noun is inaccurate.
The records are intact, chain-verified and RECOVERABLE -- merely uncommitted.
The property being guarded is real; the word overstates the damage. Recorded
in the test's docstring rather than silently reworded.


