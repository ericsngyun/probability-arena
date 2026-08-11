"""KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A1/A7 -- queue-get/disposition gap,
RETIRED.

This file used to target the true (semantically-located) window between
`SegmentWriter._run`'s `queue.get()` returning and
`self.accounting.dequeued += 1` executing -- a real, then-uncovered gap in
the queue-based writer's own accounting. `_run`, the background writer
thread, `queue.Queue`, and `WriterAccounting.dequeued`/
`dequeue_disposition_gap()` are RETIRED (KALSHI-ARCHIVE-REPLAY-INTEGRITY-001
A1): `SegmentWriter.submit` now canonicalises, chains and writes one record
entirely inside a single lock-held call on the caller's own thread, so there
is no queue-get boundary, and no gap around one, left to test.

See `tests/test_kalshi_meta_runtime_independent_accounting_001.py`'s module
docstring for the full mapping from this file's old properties to their
synchronous-era replacements, and `tests/meta_runtime/queue_gap_locator.py`
for why the locator this file depended on has no successor to locate against.
"""

from __future__ import annotations
