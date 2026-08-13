"""KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A1/A7 -- RETIRED.

This module used to provide `ControlledAdmission` (an instance-level wrapper
holding a producer thread inside `SegmentWriter._admit`),
`SlowIterationMapping` (a genuinely slow-iterating Mapping), and
`BusyThreadPool` (CPU-bound contending threads) -- instrumentation for
reproducing the queue-based writer's admission/close race: a producer
genuinely still executing inside `_admit` while `close()` -> `_seal_
admissions` gave up waiting for `_inflight` to reach zero.

`_admit`, `_seal_admissions`, `_inflight`, and the asynchronous admission
step they all existed to instrument do not exist any more (KALSHI-ARCHIVE-
REPLAY-INTEGRITY-001 A1): `SegmentWriter.submit` canonicalises, chains and
writes one record entirely inside a single lock-held call. See
`tests/test_kalshi_meta_runtime_admission_close_race_001.py`'s own
docstring for the full old-property -> replacement-property mapping and its
replacement tests, which use `pre_write_hook` (a plain, already-existing
test seam) instead of the instrumentation this module provided.
"""

from __future__ import annotations
