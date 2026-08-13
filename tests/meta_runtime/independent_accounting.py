"""KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A7 -- two INDEPENDENTLY SOURCED
observations of what a segment writer actually, durably disposed of.

RETIRED AND REPLACED, not merely edited. The original version of this module
(KALSHI-ARCHIVE-VERIFICATION-META-001 A7) targeted a specific gap in the
queue-based writer: `SegmentWriter._run`'s dequeue-then-write boundary, where
an item could be irreversibly removed from the queue and then lost before
either `written` or `failed_after_accept` ever moved. That boundary, and the
background writer thread it lived on, do not exist any more (KALSHI-ARCHIVE-
REPLAY-INTEGRITY-001 A1): `submit()` canonicalises and writes a record
entirely within one lock-held call, on the caller's own thread, so there is
no "irreversibly dequeued, not yet disposed of" state left to lose a record
in.

WHY THIS STILL MATTERS. `WriterAccounting.accepted` is still DERIVED
(`written + failed_after_accept`), so `disposition_holds()` is still TRUE BY
CONSTRUCTION -- a tautology, not an observation, exactly as before. What
changed is WHERE a genuine loss could hide from that tautology. In the
synchronous writer, `submit()` only returns `None` (accepted) AFTER
`self._fh.write(...)` has run, and `WriterAccounting.written` is incremented
in the SAME lock-held call, immediately after. There is no thread boundary
left for the two to disagree across -- but there is still a filesystem
boundary: `written` records what the WRITER believes it wrote, not what is
actually, verifiably sitting in the file. A defect in `submit()`'s own
bookkeeping (incrementing `written` without the corresponding bytes reaching
the file, or vice versa) is exactly as invisible to `disposition_holds()`'s
tautology as the old writer-thread gap was, and needs the SAME kind of
independent, second-sourced check to catch: NOT another read of
`writer.accounting`, but a read of the FILE ITSELF.

  Source 1 -- ADMISSION LEDGER, recorded OUTSIDE `SegmentWriter`, at the
             moment `submit()` returns to its caller. Exactly as before:
             "how many times did the producer's own call site see `None`
             (accepted) come back" -- a fact the producer already knows
             without reading a single attribute of the writer.

  Source 2 -- ON-DISK RECORD COUNT, read by re-decoding the segment's own
             `events.jsonl.gz` with `segment.read_segment_records` --
             completely independent of `writer.accounting`, `written`,
             `failed_after_accept`, or any in-process counter. This is the
             SAME function `close()`'s own reconciliation step uses, which
             is deliberate: an independent check that used a DIFFERENT
             reader than production's own reconciliation would prove
             nothing about whether production's reconciliation itself is
             trustworthy.

These two are RECONCILED, not each trusted alone: `ledger.accepted ==
len(read_segment_records(writer.events_path))` must hold for a non-lossy
writer. A gap means an item the ledger independently confirms was accepted
is unaccounted for in the one place that actually matters -- the durable
bytes on disk -- which is exactly the shape of loss `disposition_holds()`'s
tautology cannot express, by construction.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AdmissionLedger:
    """SOURCE 1: recorded by the TEST, at the producer's own call site --
    never reads a single attribute of the `SegmentWriter` being tested.
    This is deliberately the most naive possible observation: "how many
    times did `submit()` hand back `None`", which is exactly what a real
    producer already knows without any cooperation from the writer.
    """

    accepted: int = 0
    rejected: int = 0
    outcomes: list = field(default_factory=list)

    def record(self, reason) -> None:
        self.outcomes.append(reason)
        if reason is None:
            self.accepted += 1
        else:
            self.rejected += 1


@dataclass
class DurableDisposition:
    """SOURCE 2: what is ACTUALLY, VERIFIABLY on disk right now -- read by
    re-decoding the segment's own gzip stream, never through
    `writer.accounting` in any form (not `.written`, not `.accepted`, not
    `.disposition_holds()`). `on_disk_records` is the count `segment.
    read_segment_records` recovers from the file as it stands at the moment
    this is called; for an OPEN segment this includes whatever has been
    `flush()`ed so far (see `SegmentWriter._flush_every`), which is why the
    reconciliation tests below flush after every submit.
    """

    on_disk_records: int

    @property
    def total(self) -> int:
        return self.on_disk_records


def read_durable_disposition(writer) -> DurableDisposition:
    """Re-decode `writer.events_path` directly. Deliberately the SAME reader
    function production's own `close()`-time reconciliation uses
    (`segment.read_segment_records`) -- an independent check that used a
    DIFFERENT decoder than production would prove nothing about whether
    production's own reconciliation step can be trusted; the independence
    this module provides is in the SOURCE (the file, not `writer.
    accounting`), not in re-implementing a second parser that is merely
    intended to agree with the first.
    """
    from app.realtime import segment as sg

    records = sg.read_segment_records(writer.events_path)
    return DurableDisposition(on_disk_records=len(records))


@dataclass
class ReconciliationReport:
    ledger_accepted: int
    durable_total: int
    matches: bool
    gap: int

    def to_dict(self) -> dict:
        return {"ledger_accepted": self.ledger_accepted,
               "durable_total": self.durable_total,
               "matches": self.matches, "gap": self.gap}


def reconcile(ledger: AdmissionLedger, writer) -> ReconciliationReport:
    """THE independent check. Never reads `writer.accounting.accepted`,
    `.written`, or calls `writer.accounting.disposition_holds()`/
    `.reconciles()` -- those are the tautology this function exists to be
    independent OF. Compares the producer-observed ledger against a fresh
    re-decode of the file on disk instead.
    """
    durable = read_durable_disposition(writer)
    gap = ledger.accepted - durable.total
    return ReconciliationReport(ledger.accepted, durable.total, gap == 0, gap)
