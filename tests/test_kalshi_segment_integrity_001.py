"""KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 Gates 2-7.

Chained records, authoritative manifests, segment lifecycle, single-writer
ownership and crash consistency. No network, no SQLite, no credential.
"""

from __future__ import annotations

import gzip
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.realtime import canonical as cn
from app.realtime import segment as sg


def _init_archive(root, environment="demo"):
    """Archives are brought into existence EXPLICITLY, exactly as an operator does.

    The collector cannot do this, and that is the point: "the head is missing,
    therefore this is a new archive" was the inference that let a rebuilt
    history certify its own deletions. Tests initialize on purpose.
    """
    from app.realtime import archive_head as _ah
    try:
        _ah.initialize_archive(Path(root), environment,
                               archive_identity="kalshi-realtime")
    except _ah.ArchiveHeadError:
        pass                       # already initialized in this test
    return root


def _arch(root, **kw):
    from app.realtime import archive as _ar
    _init_archive(root, kw.get("environment", "demo"))
    return _ar.EventArchive(root, **kw)


UTC = timezone.utc
NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
ENV = "demo"
SEG = "seg-0001"


def fields(i, ticker="KXA", seq=None, mtype="orderbook_delta"):
    return {
        "connection_generation": 1,
        "subscription_id": 4,
        "subscription_generation": 1,
        "message_type": mtype,
        "market_ticker": ticker,
        "seq": seq if seq is not None else i,
        "received_at_utc": cn.canonical_datetime(NOW + timedelta(milliseconds=i)),
        "received_monotonic_ns": 1_000_000 + i,
        "raw_event": {"market_ticker": ticker, "price_dollars": "0.5100",
                      "delta_fp": "1.00", "side": "no", "ts_ms": 1786150148065 + i},
        "normalized_event": {"venue_side": "no", "raw_price_units": 5100},
    }


def write_segment(root, n=6, segment_id=SEG, meta=None):
    _init_archive(root)
    w = sg.SegmentWriter(root, environment=ENV, segment_id=segment_id,
                         partition_identity="date=2026-08-08/hour=12",
                         subscription_metadata=meta or {"market_tickers": ["KXA"]})
    for i in range(n):
        assert w.submit(fields(i)) is None
    manifest = w.close()
    return w, manifest


def records_of(w):
    return sg.read_segment_records(w.events_path)


def rewrite(w, records):
    """Rewrite the event file with the given records, as an attacker would."""
    with gzip.open(w.events_path, "wb") as fh:
        for r in records:
            fh.write(cn.canonical_bytes(r) + b"\n")


# --- Gate 2: record envelope -------------------------------------------------------
class TestRecordEnvelope:
    def test_record_carries_every_declared_field(self, tmp_path):
        w, _ = write_segment(tmp_path, n=1)
        rec = records_of(w)[0]
        for f in sg.REQUIRED_RECORD_FIELDS:
            assert f in rec, f
        assert rec["receive_ordinal"] == 0
        assert rec["segment_id"] == SEG and rec["environment"] == ENV

    def test_digest_binds_every_semantic_field(self, tmp_path):
        w, _ = write_segment(tmp_path, n=1)
        rec = records_of(w)[0]
        for f in sg.RECORD_FIELDS:
            mutated = dict(rec)
            original = mutated[f]
            mutated[f] = "MUTATED" if not isinstance(original, int) else (original or 0) + 7
            if mutated[f] == original:
                continue
            assert not sg.verify_record_self_digest(mutated), f"{f} is not bound"

    def test_genesis_is_bound_to_the_segment(self):
        """A constant sentinel would let record #1 of one segment be spliced
        into another segment's head and still chain."""
        a = sg.genesis_digest(segment_id="seg-a", environment=ENV)
        b = sg.genesis_digest(segment_id="seg-b", environment=ENV)
        c = sg.genesis_digest(segment_id="seg-a", environment="production")
        assert a != b and a != c

    @pytest.mark.parametrize("mutation,match", [
        ({"schema_version": 99}, "schema_version"),
        ({"canonical_schema_version": 99}, "canonical_schema_version"),
        ({"environment": "staging"}, "environment"),
        ({"receive_ordinal": "0"}, "receive_ordinal"),
        ({"record_digest": 123}, "must be a string"),
        ({"received_at_utc": "2026-08-08T12:00:00Z"}, "received_at_utc"),
    ])
    def test_schema_parsing_fails_closed(self, mutation, match, tmp_path):
        w, _ = write_segment(tmp_path, n=1)
        rec = dict(records_of(w)[0])
        rec.update(mutation)
        with pytest.raises(sg.RecordSchemaError, match=match):
            sg.parse_record(rec)

    def test_a_future_version_is_refused_not_tolerated(self, tmp_path):
        w, _ = write_segment(tmp_path, n=1)
        rec = dict(records_of(w)[0])
        rec["schema_version"] = sg.RECORD_SCHEMA_VERSION + 1
        with pytest.raises(sg.RecordSchemaError):
            sg.parse_record(rec)


# --- Gate 3: ordered chain ---------------------------------------------------------
class TestOrderedChain:
    def test_a_clean_chain_verifies(self, tmp_path):
        w, _ = write_segment(tmp_path, n=6)
        v = sg.verify_chain(records_of(w), segment_id=SEG, environment=ENV)
        assert v.ok and v.record_count == 6
        assert v.first_record_digest and v.last_record_digest

    @pytest.mark.parametrize("attack", [
        "delete_middle", "delete_tail", "insert", "duplicate", "reorder", "mutate",
    ])
    def test_every_ordering_attack_is_detected(self, attack, tmp_path):
        w, _ = write_segment(tmp_path, n=6)
        recs = records_of(w)
        if attack == "delete_middle":
            recs = recs[:3] + recs[4:]
        elif attack == "delete_tail":
            recs = recs[:-1]
        elif attack == "insert":
            recs = recs[:3] + [dict(recs[2])] + recs[3:]
        elif attack == "duplicate":
            recs = recs[:3] + [recs[2]] + recs[3:]
        elif attack == "reorder":
            recs = recs[:2] + [recs[3], recs[2]] + recs[4:]
        elif attack == "mutate":
            recs = [dict(r) for r in recs]
            recs[3]["raw_event"] = {"tampered": True}
        v = sg.verify_chain(recs, segment_id=SEG, environment=ENV)
        if attack == "delete_tail":
            # A dropped TAIL still chains; the manifest's record_count is what
            # catches it, which is exactly why the manifest is authoritative.
            assert v.ok and v.record_count == 5
        else:
            assert not v.ok, f"{attack} was not detected"

    def test_the_stream_digest_changes_for_every_attack(self, tmp_path):
        w, _ = write_segment(tmp_path, n=6)
        recs = records_of(w)
        base = sg.verify_chain(recs, segment_id=SEG, environment=ENV).ordered_stream_digest
        for name, mutated in (
                ("delete", recs[:-1]),
                ("reorder", recs[:2] + [recs[3], recs[2]] + recs[4:]),
                ("duplicate", recs + [recs[-1]])):
            v = sg.verify_chain(mutated, segment_id=SEG, environment=ENV)
            # Either the chain refuses it outright, or the fold differs. A
            # trailing duplicate breaks the chain at the last link, so the fold
            # up to that point legitimately still matches — detection is the
            # property that matters, not that every attack moves the digest.
            assert (not v.ok) or v.ordered_stream_digest != base, name

    def test_a_wrong_genesis_is_detected(self, tmp_path):
        w, _ = write_segment(tmp_path, n=3)
        v = sg.verify_chain(records_of(w), segment_id="other-seg", environment=ENV)
        assert not v.ok

    def test_a_lone_surrogate_tamper_is_a_typed_refusal_not_a_crash(self, tmp_path):
        """KALSHI-ARCHIVE-CORE-REMEDIATION-003 defect C: a lone UTF-16
        surrogate (`"\\ud800"`) is legal JSON and round-trips from a
        committed file. Before the fix, a record whose `raw_event` had been
        tampered to carry one made `verify_record_self_digest` ->
        `digest_hex` -> `canonical_bytes` raise a raw `UnicodeEncodeError`
        (a `ValueError`) instead of `CanonicalError`, which crashed
        `verify_chain` -- it catches only `CanonicalError`/
        `RecordSchemaError` -- exactly the "tamper-evidence path that
        crashes on attacker-controlled input" anti-pattern `canonical.py`'s
        own docstring names."""
        w, _ = write_segment(tmp_path, n=6)
        recs = [dict(r) for r in records_of(w)]
        recs[3]["raw_event"] = {"evil": "\ud800"}
        v = sg.verify_chain(recs, segment_id=SEG, environment=ENV)
        assert not v.ok
        assert "surrogate" in (v.reason or ""), v.reason


# --- Gate 4: manifest --------------------------------------------------------------
class TestManifest:
    def test_a_closed_segment_has_an_authoritative_manifest(self, tmp_path):
        w, manifest = write_segment(tmp_path, n=5)
        assert w.state is sg.SegmentState.CLOSED
        assert w.manifest_path.exists()
        for f in sg.MANIFEST_FIELDS:
            assert f in manifest, f
        assert manifest["record_count"] == 5
        assert sg.verify_manifest_self_digest(manifest)
        assert sg.verify_segment(w.dir, environment=ENV).valid

    def test_caller_mutation_of_subscription_metadata_after_construction_is_inert(
            self, tmp_path):
        """KALSHI-ARCHIVE-CORE-REMEDIATION-003 defect E: `self.
        subscription_metadata = metadata` used to retain the CALLER's live
        reference. Before the fix, mutating the caller's dict AFTER
        constructing the writer -- even benignly -- silently landed in the
        published manifest with a self-consistent digest that verified
        VALID, and a hostile mutation could inject content that was never
        admitted through `non_canonical_reason`. This proves the accepted
        representation is an immutable SNAPSHOT, not a shared reference."""
        _init_archive(tmp_path)
        meta = {"market_tickers": ["KXA"]}
        w = sg.SegmentWriter(tmp_path, environment=ENV, segment_id=SEG,
                             partition_identity="date=2026-08-08/hour=12",
                             subscription_metadata=meta)
        # Mutate the CALLER's object after construction -- a relabel and an
        # injection, neither of which should ever be able to reach the
        # writer or the manifest it will publish.
        meta["market_tickers"] = ["TAMPERED"]
        meta["injected"] = "never admitted through non_canonical_reason"
        assert w.subscription_metadata == {"market_tickers": ["KXA"]}, (
            "the writer's stored subscription_metadata must not reflect a "
            "mutation made to the caller's object after construction")
        for i in range(3):
            assert w.submit(fields(i)) is None
        manifest = w.close()
        assert manifest["subscription_metadata"] == {
            "market_tickers": ["KXA"]}, (
            "the PUBLISHED manifest must hold the value validated at "
            "construction, not whatever the caller's object mutated into "
            "afterward")
        assert sg.verify_segment(w.dir, environment=ENV).valid

    @pytest.mark.parametrize("attack", [
        "delete_one_complete_record", "delete_event_file", "delete_manifest",
        "edit_manifest_count", "swap_manifest", "truncate_file", "flip_byte",
    ])
    def test_every_manifest_attack_is_invalid(self, attack, tmp_path):
        w, _ = write_segment(tmp_path, n=6)
        if attack == "delete_one_complete_record":
            rewrite(w, records_of(w)[:-1])
        elif attack == "delete_event_file":
            w.events_path.unlink()
        elif attack == "delete_manifest":
            w.manifest_path.unlink()
        elif attack == "edit_manifest_count":
            m = cn.parse_canonical(w.manifest_path.read_bytes())
            m["record_count"] = 5
            w.manifest_path.write_bytes(cn.canonical_bytes(m))
        elif attack == "swap_manifest":
            other = tmp_path / "other"
            w2, _ = write_segment(other, n=6, segment_id="seg-0002")
            w.manifest_path.write_bytes(w2.manifest_path.read_bytes())
        elif attack == "truncate_file":
            w.events_path.write_bytes(w.events_path.read_bytes()[:-40])
        elif attack == "flip_byte":
            b = bytearray(w.events_path.read_bytes())
            b[len(b) // 2] ^= 0xFF
            w.events_path.write_bytes(bytes(b))
        v = sg.verify_segment(w.dir, environment=ENV)
        assert not v.valid, f"{attack} verified as valid"
        assert v.state is sg.SegmentState.INVALID
        assert v.reasons

    def test_a_missing_manifest_is_never_reconstructed(self, tmp_path):
        w, _ = write_segment(tmp_path, n=4)
        w.manifest_path.unlink()
        v = sg.verify_segment(w.dir, environment=ENV)
        assert not v.valid
        assert not w.manifest_path.exists(), "verification must not write a manifest"

    def test_wrong_environment_is_invalid(self, tmp_path):
        w, _ = write_segment(tmp_path, n=3)
        v = sg.verify_segment(w.dir, environment="production")
        assert not v.valid and not v.environment_valid

    def test_subscription_metadata_tamper_is_detected(self, tmp_path):
        w, _ = write_segment(tmp_path, n=3, meta={"market_tickers": ["KXA"]})
        m = cn.parse_canonical(w.manifest_path.read_bytes())
        m["subscription_metadata"] = {"market_tickers": ["KXA", "INJECTED"]}
        w.manifest_path.write_bytes(cn.canonical_bytes(m))
        v = sg.verify_segment(w.dir, environment=ENV)
        assert not v.valid

    def test_segment_id_cannot_escape_its_directory(self, tmp_path):
        for bad in ("../escape", "a/b", "", "x" * 200, ".hidden"):
            with pytest.raises(sg.SegmentError, match="safe path component"):
                sg.SegmentWriter(tmp_path, environment=ENV, segment_id=bad,
                                 partition_identity="p")


# --- Gate 5/7: lifecycle and crash consistency -------------------------------------
class TestLifecycleAndCrash:
    def test_state_progression(self, tmp_path):
        _init_archive(tmp_path)
        w = sg.SegmentWriter(tmp_path, environment=ENV, segment_id=SEG,
                             partition_identity="p")
        assert w.state is sg.SegmentState.OPEN and w.accepting
        w.submit(fields(0))
        w.close()
        assert w.state is sg.SegmentState.CLOSED
        # A1: SEGMENT_NOT_OPEN once close() has actually returned;
        # SHUTDOWN_IN_PROGRESS is reserved for a caller racing an
        # in-progress close() (see RejectReason's docstring).
        assert w.submit(fields(1)) is sg.RejectReason.SEGMENT_NOT_OPEN

    def test_an_open_segment_is_not_canonical_evidence(self, tmp_path):
        _init_archive(tmp_path)
        w = sg.SegmentWriter(tmp_path, environment=ENV, segment_id=SEG,
                             partition_identity="p")
        w.submit(fields(0))
        import time
        time.sleep(0.3)
        v = sg.verify_segment(w.dir, environment=ENV, allow_open=True)
        assert not v.valid and v.state is sg.SegmentState.OPEN
        w.close()

    def test_crash_before_manifest_publication_is_never_closed(self, tmp_path):
        """The manifest is the commit record, so its absence must read as
        uncommitted rather than as a valid segment."""
        w, _ = write_segment(tmp_path, n=4)
        w.manifest_path.unlink()                       # simulate crash pre-publish
        v = sg.verify_segment(w.dir, environment=ENV, allow_open=True)
        assert v.state is not sg.SegmentState.CLOSED
        assert not v.valid

    def test_a_partial_manifest_temp_file_is_not_a_manifest(self, tmp_path):
        w, _ = write_segment(tmp_path, n=3)
        real = w.manifest_path.read_bytes()
        w.manifest_path.unlink()
        (w.dir / (sg.MANIFEST_FILENAME + sg.MANIFEST_TEMP_SUFFIX)).write_bytes(real[:20])
        v = sg.verify_segment(w.dir, environment=ENV, allow_open=True)
        assert not v.valid

    def test_torn_tail_keeps_every_complete_prior_record(self, tmp_path):
        w, _ = write_segment(tmp_path, n=8)
        w.events_path.write_bytes(w.events_path.read_bytes()[:-6])
        recs = sg.read_segment_records(w.events_path)
        assert len(recs) >= 7, "a torn tail must cost only the incomplete record"
        assert sg.verify_chain(recs, segment_id=SEG, environment=ENV).ok

    def test_manifest_publication_is_atomic(self, tmp_path):
        d = tmp_path / "seg"
        d.mkdir()
        m = sg.build_manifest(
            environment=ENV, segment_id=SEG, partition_identity="p",
            opened_at=cn.canonical_datetime(NOW), closed_at=cn.canonical_datetime(NOW),
            record_count=0, first_record_digest=None, last_record_digest=None,
            ordered_stream_digest="x", event_file_size_bytes=0,
            event_file_sha256="y", subscription_metadata={})
        sg.publish_manifest(d, m)
        assert (d / sg.MANIFEST_FILENAME).exists()
        assert not (d / (sg.MANIFEST_FILENAME + sg.MANIFEST_TEMP_SUFFIX)).exists()


# --- Gate 6: single writer ---------------------------------------------------------
class TestSingleWriter:
    def test_accounting_reconciles_under_concurrency(self, tmp_path):
        _init_archive(tmp_path)
        w = sg.SegmentWriter(tmp_path, environment=ENV, segment_id=SEG,
                             partition_identity="p", queue_maxsize=2048)
        per, producers = 500, 6
        errors = []

        def produce(pid):
            try:
                for i in range(per):
                    w.submit(fields(pid * per + i))
            except Exception as exc:                    # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=produce, args=(p,)) for p in range(producers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        w.close()
        assert not errors
        acc = w.accounting
        assert acc.attempted == per * producers
        assert acc.admission_holds() and acc.disposition_holds(), acc.to_dict()
        assert acc.clean(), acc.to_dict()
        assert acc.written == len(records_of(w))
        assert sg.verify_segment(w.dir, environment=ENV).valid

    def test_retired_queue_knobs_are_accepted_and_have_no_effect(self, tmp_path):
        """KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A8 -- REPLACES
        `test_queue_overflow_is_rejected_not_dropped`.

        That test submitted 400 events to a writer built with
        `queue_maxsize=1, enqueue_timeout_s=0.01` and asserted `all(r is
        RejectReason.ENQUEUE_TIMEOUT for r in rejected)` -- guarded by `if
        rejected:`. A1 deleted the queue, so nothing can ever be rejected
        for enqueue timeout and `rejected` is now ALWAYS empty: the only
        assertion about the behaviour it was named for had become
        unreachable, and the test passed by doing nothing.

        What is genuinely worth pinning is the compatibility decision A1
        actually made: the two retired keyword arguments are still ACCEPTED
        (so no existing caller raises `TypeError`) and have NO effect (every
        event is written, none rejected). This test fails if either half of
        that changes."""
        _init_archive(tmp_path)
        w = sg.SegmentWriter(tmp_path, environment=ENV, segment_id=SEG,
                             partition_identity="p", queue_maxsize=1,
                             enqueue_timeout_s=0.01)
        reasons = [w.submit(fields(i)) for i in range(400)]
        manifest = w.close()
        assert [r for r in reasons if r is not None] == [], (
            "the retired queue knobs must not reject anything -- there is no "
            "queue left for them to bound")
        assert w.accounting.reconciles(), w.accounting.to_dict()
        assert w.accounting.written == 400
        assert manifest["record_count"] == 400
        # `RejectReason.ENQUEUE_TIMEOUT` is still DEFINED (retired, not
        # deleted, like the two keyword arguments). What must be true is
        # that nothing can ever PRODUCE it.
        assert sg.RejectReason.ENQUEUE_TIMEOUT.value not in w.accounting.rejections

    def test_a_failed_writer_stops_accepting_evidence(self, tmp_path):
        _init_archive(tmp_path)
        w = sg.SegmentWriter(tmp_path, environment=ENV, segment_id=SEG,
                             partition_identity="p")
        w._writer_error = RuntimeError("disk full")
        w.state = sg.SegmentState.INVALID
        assert not w.healthy and not w.accepting
        assert w.submit(fields(0)) is sg.RejectReason.WRITER_FAILED
        assert w.accounting.reconciles()

    def test_submit_writes_directly_but_only_inside_the_serialization_lock(self):
        """Structural, INVERTED for KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A1.

        The OLD invariant this test asserted -- `submit()` must never touch
        `_fh`/`write`/`gzip` at all -- was the queue-based design's core
        safety property: a producer handed its event to a bounded queue and
        ONE background writer thread was the only code that ever touched the
        file descriptor. A1 removes that queue and writer thread ON PURPOSE
        (see `SegmentWriter`'s class docstring): `submit()` now IS the
        writer, canonicalising and writing on the caller's own thread. That
        makes the old assertion structurally FALSE by design, not a
        regression to patch around -- asserting it here would be asserting
        the milestone did not happen.

        The property that actually matters now is not "submit() never
        touches the file" but "submit() only ever touches the file while
        holding `self._lock`" -- the SAME single fact that makes one
        segment, one writer, still true without a background thread to
        enforce it structurally. Verified by AST: every `self._fh` /
        `write(` / `flush(` reference inside `submit()` must be lexically
        nested inside a `with self._lock:` block.
        """
        import ast
        from pathlib import Path

        # Locate `submit` by parsing the FILE, not via inspect.getsource --
        # see the historical note this test used to carry: getsource
        # resolves through linecache, which can desync from the already-
        # imported code object's line numbers under a mid-suite edit.
        module = ast.parse(Path(sg.__file__).read_text())
        cls = next(n for n in module.body
                   if isinstance(n, ast.ClassDef) and n.name == "SegmentWriter")
        tree = next(n for n in cls.body
                    if isinstance(n, ast.FunctionDef) and n.name == "submit")

        # THE INVERTED POSITIVE ASSERTION: submit() must actually reach the
        # file -- if it does not, A1's own architecture (a caller must never
        # be told ACCEPTED before the canonical writer owns the event) is not
        # implemented by this function at all.
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        for expected in ("write", "_fh"):
            assert expected in names, (
                f"submit() no longer touches {expected!r} -- either the "
                "synchronous write moved elsewhere, or this test needs "
                "updating to point at wherever it now lives")

        # THE LOCK-CONTAINMENT ASSERTION: walk the function body; every
        # `with` statement whose context expression is `self._lock` opens a
        # region, and every guarded reference must be lexically inside at
        # least one such region.
        #
        # KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A8: the guarded set now also
        # covers `self.state` and `self.accounting`, not only the file
        # handle. Without them, the single most plausible "optimisation"
        # anyone would propose against this function -- read `self.state`
        # BEFORE taking the lock so a closed/invalid writer can reject
        # without contending -- reintroduces the exact TOCTOU race the
        # synchronous design exists to eliminate (`close()` moves `state` to
        # CLOSING between the unlocked read and the locked write, and the
        # producer is told ACCEPTED for a segment that is already sealing),
        # and NO test in this suite would catch it. `accounting` is included
        # for the same reason from the other direction: a counter mutated
        # outside the lock can be lost or double-applied under concurrent
        # producers, which is how the identity used to drift.
        GUARDED_ATTRS = ("_fh", "write", "flush", "state", "accounting")

        def _is_self_lock(expr) -> bool:
            return (isinstance(expr, ast.Attribute) and expr.attr == "_lock"
                    and isinstance(expr.value, ast.Name) and expr.value.id == "self")

        def _walk(node, inside_lock: bool):
            if isinstance(node, ast.With):
                lock_here = any(_is_self_lock(item.context_expr)
                                for item in node.items)
                for child in node.body:
                    _walk(child, inside_lock or lock_here)
                return
            if isinstance(node, ast.Attribute) and node.attr in GUARDED_ATTRS:
                # Only `self.<attr>` -- a local `record.state` or some other
                # object's `.write` is not this function's business.
                is_self = (isinstance(node.value, ast.Name)
                           and node.value.id == "self")
                if node.attr in ("write", "flush"):
                    # These are method NAMES, reached as `<something>.write`;
                    # requiring `self.` would miss `self._fh.write(...)`.
                    is_self = True
                if is_self:
                    assert inside_lock, (
                        f"submit() references .{node.attr} at line "
                        f"{node.lineno} outside any `with self._lock:` block "
                        "-- the write path, the state check and the "
                        "accounting must ALL be fully contained by the "
                        "serialization lock, or the check-then-act race the "
                        "synchronous design removes comes straight back")
            for child in ast.iter_child_nodes(node):
                _walk(child, inside_lock)

        for stmt in tree.body:
            _walk(stmt, inside_lock=False)

        # And the guard must not be vacuous: submit() really does read
        # `self.state` and mutate `self.accounting`, so if either name stops
        # appearing the assertion above has quietly stopped protecting
        # anything.
        for expected in ("state", "accounting"):
            assert expected in names, (
                f"submit() no longer references self.{expected} -- the "
                "lock-containment guard above is now vacuous for it")


# --- verification surface ----------------------------------------------------------
class TestArchiveVerification:
    def test_archive_verdict_is_fail_closed_on_empty(self, tmp_path):
        out = sg.verify_archive(tmp_path, environment=ENV)
        assert out["verdict"] == "INVALID"
        assert out["segments"] == 0

    def test_one_invalid_segment_invalidates_the_archive(self, tmp_path):
        write_segment(tmp_path, n=3, segment_id="seg-a")
        w2, _ = write_segment(tmp_path, n=3, segment_id="seg-b")
        assert sg.verify_archive(tmp_path, environment=ENV)["verdict"] == "VALID"
        w2.manifest_path.unlink()
        out = sg.verify_archive(tmp_path, environment=ENV)
        # A manifest-less segment reads as OPEN during a scan — nothing on disk
        # distinguishes "still being written" from "its commit record was
        # deleted". Either way it is not committed evidence, so the archive
        # verdict is INVALID and the segment stops counting as closed.
        assert out["verdict"] == "INVALID"
        assert out["closed_segments"] == 1
        assert out["invalid_segments"] + out["open_segments"] == 1

    def test_durable_uncommitted_residue_is_typed_and_visible_not_invisible(
            self, tmp_path):
        """KALSHI-ARCHIVE-CORE-REMEDIATION-003 defect G (part 2): a segment
        with durable, chain-valid records that never published a manifest
        (a writer crash before close(), or -- before defect G part 1's fix
        -- a permanently bricked `_inflight` seal) used to be visible ONLY
        as a bare id in `uncommitted_segments`; `records_read` summed only
        the COMMITTED segments, so it read 0 regardless of how much real
        evidence was sitting on disk, and `verdict`/`reasons` gave no
        indication anything needed attention. This proves the residue is
        now a TYPED, inspectable state -- without being automatically
        blessed as committed evidence (still VALID, still not part of
        `records_expected`/`segments`)."""
        _init_archive(tmp_path)
        w = sg.SegmentWriter(tmp_path, environment=ENV, segment_id="seg-crashed",
                             partition_identity="p", commit_to_head=False,
                             flush_every=1)
        for i in range(20):
            assert w.submit(fields(i)) is None
        # Simulate a crash: the writer thread has flushed real records, but
        # close()/publish_manifest() never ran -- no manifest exists.
        import time
        deadline = time.monotonic() + 2.0
        while w.accounting.written < 20 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not w.manifest_path.exists()

        out = sg.verify_archive(tmp_path, environment=ENV)
        assert out["verdict"] == "VALID", (
            "an in-progress/crashed-but-uncommitted segment must not, by "
            f"itself, invalidate the archive: {out}")
        assert out["uncommitted_segments"] == ["seg-crashed"]
        assert out["uncommitted_records_present"] == 20, (
            "the durable record count on disk must be visible, not 0 -- "
            f"{out}")
        detail = out["uncommitted_segment_detail"]
        assert len(detail) == 1
        assert detail[0]["segment_id"] == "seg-crashed"
        assert detail[0]["records_read"] == 20
        assert any("UNCOMMITTED_SEGMENT_RESIDUE" in w_ for w_ in out["warnings"]), (
            f"expected a loud, non-gating warning naming the residue: {out}")
