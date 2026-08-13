"""KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A1/A7 -- RETIRED.

This module used to locate the exact source line of `SegmentWriter._run`'s
dequeue-then-disposition gap (`self.accounting.dequeued += 1`), for a fault
injector to target precisely. `_run`, the background writer thread, the
bounded `queue.Queue`, and `WriterAccounting.dequeued`/
`dequeue_disposition_gap()` do not exist any more: `SegmentWriter.submit`
canonicalises, chains and writes one record entirely inside a single
lock-held call, on the caller's own thread (KALSHI-ARCHIVE-REPLAY-INTEGRITY-
001 A1). There is no queue-get boundary left to locate a gap around.

See `tests/test_kalshi_meta_runtime_independent_accounting_001.py`'s module
docstring for the full old-property -> replacement-property mapping; the
class of defect this module's locator existed to help catch (a durable
accounting fact recorded on one side of an asynchronous boundary while the
underlying evidence was lost on the other) is now caught, for the boundary
that actually remains (a write-time OS failure), by that file's
`TestSubmitFailureIsImmediatelyVisible` and
`TestPlantedBadAccountingFailsTheMetaTest`.
"""

from __future__ import annotations
