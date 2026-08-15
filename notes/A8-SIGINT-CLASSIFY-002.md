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
