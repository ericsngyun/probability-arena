"""KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 — reproduction of known archive defects.

Written BEFORE any implementation change, so each test discriminates the
current failure from the intended behaviour rather than describing whatever the
code happens to do. Findings 1-9 are expected to FAIL against 2c8f75b; 10-13 are
expected to PASS and must not regress.

No network, no SQLite, no credential.
"""

from __future__ import annotations

import gzip
import json
import multiprocessing as mp
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.realtime import archive as ar
from app.realtime import book as bk

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
M1, M2 = "KXA-TEST", "KXB-TEST"


def envelope(*, seq, ticker=M1, etype="orderbook_delta", sid=4, ts_ms=None,
             when=None, side="no", price="0.5100", amount="1.00"):
    """An envelope built the way the collector actually builds one."""
    if etype == "orderbook_snapshot":
        msg = {"market_ticker": ticker, "market_id": "mid-1",
               "yes_dollars_fp": [["0.4700", "5.00"]],
               "no_dollars_fp": [["0.5100", "5.00"]]}
    else:
        msg = {"market_ticker": ticker, "market_id": "mid-1",
               "price_dollars": price, "delta_fp": amount, "side": side}
    if ts_ms is not None:
        msg["ts_ms"] = ts_ms
    message = {"type": etype, "sid": sid, "seq": seq, "msg": msg}
    return bk.make_envelope(
        venue="kalshi", environment="demo", channel="orderbook_delta",
        message=message, receive_time=(when or NOW) + timedelta(milliseconds=seq),
        receive_mono=1_000_000 + seq)


def written_archive(tmp_path, envelopes):
    a = ar.EventArchive(tmp_path, environment="demo")
    for e in envelopes:
        a.append(e)
    return a


def only_file(tmp_path) -> Path:
    return next(tmp_path.rglob("events.jsonl.gz"))


# --- 1-2: the digest/float round-trip defect ---------------------------------------
class TestVenueTimestampRoundTrip:
    def test_1_a_record_with_ts_ms_survives_its_own_digest(self, tmp_path):
        """`data_age_ms` is written as a bare float, re-read as Decimal, and
        re-canonicalised through `default=str` as a QUOTED string — so the
        recomputed digest can never match and the record is dropped as if it
        had been tampered with."""
        a = written_archive(tmp_path, [envelope(seq=1, ts_ms=1_786_150_148_065)])
        assert a.read_all(), "a record with a venue timestamp must be readable"
        assert a.verify()["intact"] is True
        assert a.verify()["mismatched"] == []

    def test_2_five_thousand_realistic_deltas_are_readable(self, tmp_path):
        base = 1_786_150_148_065
        a = written_archive(tmp_path, [
            envelope(seq=i, ts_ms=base + i * 37,
                     amount=f"{(i % 17) + 1}.00")
            for i in range(1, 5001)])
        recs = a.read_all()
        assert len(recs) == 5000, f"only {len(recs)}/5000 readable"
        assert a.verify()["records"] == 5000


# --- 3-5: deletion and the missing manifest ----------------------------------------
class TestDeletionDetection:
    def test_3_deleting_one_complete_record_invalidates_the_archive(self, tmp_path):
        a = written_archive(tmp_path, [envelope(seq=i) for i in range(1, 7)])
        assert a.verify()["intact"] is True
        p = only_file(tmp_path)
        with gzip.open(p, "rt") as fh:
            lines = fh.read().splitlines()
        with gzip.open(p, "wt") as fh:                 # drop a COMPLETE record
            fh.write("\n".join(lines[:-1]) + "\n")
        v = a.verify()
        assert v["intact"] is False, (
            "loss of a complete prior record must fail verification")

    def test_4_deleting_the_whole_event_file_fails_closed(self, tmp_path):
        a = written_archive(tmp_path, [envelope(seq=i) for i in range(1, 7)])
        only_file(tmp_path).unlink()
        v = a.verify()
        assert v["intact"] is False, "a wiped archive must not verify as intact"
        assert v["records"] == 0

    def test_5_a_closed_segment_produces_an_authoritative_manifest(self, tmp_path):
        """`MANIFEST_FILENAME` is declared and never written, so nothing pins
        the expected record count — which is why deletion is undetectable."""
        written_archive(tmp_path, [envelope(seq=i) for i in range(1, 4)])
        manifests = list(tmp_path.rglob(ar.MANIFEST_FILENAME))
        assert manifests, f"no {ar.MANIFEST_FILENAME} written for the segment"
        m = json.loads(manifests[0].read_text())
        for field in ("record_count", "ordered_stream_digest", "environment",
                      "schema_version", "first_record_digest",
                      "last_record_digest"):
            assert field in m, f"manifest missing {field}"
        assert m["record_count"] == 3


# --- 6: concurrent writers ---------------------------------------------------------
def _producer(args):
    root, n, pid = args
    a = ar.EventArchive(root, environment="demo")
    for i in range(n):
        a.append(envelope(seq=pid * 10_000 + i, ts_ms=None))
    return n


class TestSingleWriterOwnership:
    def test_6_six_concurrent_producers_lose_nothing_silently(self, tmp_path):
        """`gzip.open(path, "at")` per record with no lock: concurrent members
        interleave and the reader recovers a fraction, reported as a single
        truncated record."""
        per, procs = 120, 6
        with mp.Pool(procs) as pool:
            pool.map(_producer, [(str(tmp_path), per, i) for i in range(procs)])
        a = ar.EventArchive(tmp_path, environment="demo")
        recs = a.read_all()
        v = a.verify()
        expected = per * procs
        accounted = len(recs) + v.get("truncated_records", 0)
        assert len(recs) == expected, (
            f"{len(recs)}/{expected} survived; "
            f"accounted {accounted} — silent loss of {expected - accounted}")


# --- 7: replay ownership -----------------------------------------------------------
class TestReplayOwnership:
    def test_7_cross_market_injection_is_refused_on_replay(self, tmp_path):
        """`replay` builds the router with no `market_tickers`, disabling the
        one guard that exists, on exactly the path it protects."""
        recs = [envelope(seq=1, etype="orderbook_snapshot").to_dict(),
                envelope(seq=2).to_dict(),
                envelope(seq=3).to_dict()]
        recs[2]["market_ticker"] = "INJECTED-MARKET"
        recs[2]["raw"]["msg"]["market_ticker"] = "INJECTED-MARKET"
        out = ar.replay(recs)
        assert out["events_rejected"] >= 1, "injected market must be rejected"
        assert "INJECTED-MARKET" not in out["checksums"]


# --- 8-9: snapshot and duplicate semantics -----------------------------------------
class TestSnapshotIdempotency:
    def test_8_a_byte_identical_duplicate_snapshot_is_idempotent(self, tmp_path):
        """Kalshi redelivers snapshots on resubscribe, so this is the common
        case — and it currently re-applies, bumping `generation` and changing
        the checksum that replay determinism is asserted on."""
        snap = envelope(seq=1, etype="orderbook_snapshot").to_dict()
        once = ar.replay([snap])
        twice = ar.replay([snap, dict(snap)])
        assert twice["checksums"] == once["checksums"], (
            "redelivered identical snapshot changed the book checksum")

    def test_9_duplicate_events_are_counted_in_archive_statistics(self, tmp_path):
        e = envelope(seq=1)
        a = written_archive(tmp_path, [e, e])
        v = a.verify()
        assert "duplicate_count" in v, "verify() reports no duplicate accounting"
        assert v["duplicate_count"] == 1


# --- 10-13: behaviours that currently PASS and must not regress --------------------
class TestMustNotRegress:
    def test_10_tail_truncation_recovers_all_complete_prior_records(self, tmp_path):
        a = written_archive(tmp_path, [envelope(seq=i) for i in range(1, 7)])
        p = only_file(tmp_path)
        p.write_bytes(p.read_bytes()[:-5])
        recs = a.read_all()
        assert len(recs) >= 5, "a torn tail must lose only the incomplete record"

    def test_11_physical_reorder_is_detected(self, tmp_path):
        recs = [envelope(seq=i, etype=("orderbook_snapshot" if i == 1
                                       else "orderbook_delta")).to_dict()
                for i in range(1, 7)]
        good = ar.replay(recs)
        swapped = recs[:2] + [recs[3], recs[2]] + recs[4:]
        bad = ar.replay(swapped)
        assert bad["events_rejected"] > good["events_rejected"]

    def test_12_environment_label_mismatch_is_detected(self, tmp_path):
        a = written_archive(tmp_path, [envelope(seq=1)])
        p = only_file(tmp_path)
        with gzip.open(p, "rt") as fh:
            line = fh.read().strip()
        rec = json.loads(line)
        rec["environment"] = "production"
        with gzip.open(p, "wt") as fh:
            fh.write(json.dumps(rec) + "\n")
        assert a.read_all() == []
        assert a.verify()["intact"] is False

    def test_13_a_malformed_complete_record_is_detected(self, tmp_path):
        a = written_archive(tmp_path, [envelope(seq=1), envelope(seq=2)])
        p = only_file(tmp_path)
        with gzip.open(p, "rt") as fh:
            lines = fh.read().splitlines()
        lines[0] = lines[0].replace('"0.5100"', '"0.9900"')   # tamper, keep digest
        with gzip.open(p, "wt") as fh:
            fh.write("\n".join(lines) + "\n")
        assert a.verify()["intact"] is False
