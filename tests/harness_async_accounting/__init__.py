"""KALSHI-ARCHIVE-VERIFICATION-HARNESSES-001, harness A3.

Fault-injection harness for the class: an asynchronous exception arrives
inside `SegmentWriter.submit()`/admission at an instruction boundary and
breaks the writer accounting.

Nothing in this package is imported by `app/`. It is test-only, and it never
edits a production module — it observes `app.realtime.segment` from outside
using two independent injection methods (a deterministic line-boundary trace
hook, and real asynchronous exceptions: SIGINT and
`PyThreadState_SetAsyncExc`), plus a set of standalone reference
implementations used only to prove the harness itself discriminates correct
from broken accounting.
"""
