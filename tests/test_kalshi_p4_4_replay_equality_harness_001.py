"""KALSHI-P4-4 — the replay-equality harness must be able to FAIL.

A qualification harness that cannot go red is a rubber stamp. The production
run was demonstrated to flip once, by hand; this pins it so it stays true.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path


def _harness():
    spec = importlib.util.spec_from_file_location(
        "_eq", "scripts/kalshi_p4_replay_equality.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _build_archive(root: Path):
    from app.realtime import archive as ar
    from app.realtime import archive_head as ah
    from app.realtime import book as bk

    try:
        ah.initialize_archive(root, "production",
                              archive_identity="kalshi-realtime")
    except ah.ArchiveHeadError:
        pass
    arch = ar.EventArchive(root, environment="production")

    def send(etype, seq, msg):
        arch.append(bk.make_envelope(
            venue="kalshi", environment="production", channel=etype,
            message={"type": etype, "sid": 1, "seq": seq, "msg": msg},
            receive_time=datetime(2026, 8, 20, tzinfo=timezone.utc),
            receive_mono=bk.monotonic_ns()))

    send("orderbook_snapshot", 1,
         {"market_ticker": "KXTEST", "market_id": "m1",
          "yes_dollars_fp": [], "no_dollars_fp": []})
    send("orderbook_delta", 2,
         {"market_ticker": "KXTEST", "market_id": "m1",
          "price_dollars": "0.5100", "delta_fp": "5.00", "side": "no",
          "ts_ms": 1786150148065})
    arch.close()
    return arch


def _capture_json(path: Path, root: Path, checksum, publishable=True):
    from app.realtime.archive import EventArchive, replay
    recs = EventArchive(root, environment="production").read_verified()
    out = replay(recs)
    ticker = next(iter(out["checksums"]))
    payload = {
        "session_result": {"segments_committed": 1},
        "live_terminal_state": {
            "1": {"sid": 1, "carries_orderbook": True,
                  "books": {ticker: {
                      "checksum": (out["checksums"][ticker] if checksum is None
                                   else checksum),
                      "publishable": (out["publishable"][ticker] if publishable
                                      else not out["publishable"][ticker]),
                      "stats": dict(out["stats"][ticker]),
                  }}}},
    }
    path.write_text(json.dumps(payload))
    return ticker


class TestTheHarnessQualifiesAnHonestTape:
    def test_a_matching_capture_qualifies(self, tmp_path):
        root = tmp_path / "tape"
        root.mkdir()
        _build_archive(root)
        cap = tmp_path / "capture.json"
        _capture_json(cap, root, checksum=None)

        res = _harness().qualify(root, cap)
        assert res["verdict"] == "QUALIFIED", [
            c for c in res["checks"] if not c["pass"]]


class TestTheHarnessRefusesATamperedOne:
    """Anti-vacuity, the whole reason this file exists."""

    def test_a_wrong_checksum_is_caught(self, tmp_path):
        root = tmp_path / "tape"
        root.mkdir()
        _build_archive(root)
        cap = tmp_path / "capture.json"
        _capture_json(cap, root, checksum="deadbeefdeadbeef")

        res = _harness().qualify(root, cap)
        assert res["verdict"] == "NOT_QUALIFIED"
        bad = [c for c in res["checks"]
               if c["check"] == "terminal_state_equality" and not c["pass"]]
        assert bad, "the checksum mismatch was not the check that failed"

    def test_a_wrong_publishable_flag_is_caught(self, tmp_path):
        root = tmp_path / "tape"
        root.mkdir()
        _build_archive(root)
        cap = tmp_path / "capture.json"
        _capture_json(cap, root, checksum=None, publishable=False)

        res = _harness().qualify(root, cap)
        assert res["verdict"] == "NOT_QUALIFIED"

    def test_an_empty_comparison_is_a_failure_not_a_pass(self, tmp_path):
        """A harness that compared nothing must NOT report QUALIFIED."""
        root = tmp_path / "tape"
        root.mkdir()
        _build_archive(root)
        cap = tmp_path / "capture.json"
        cap.write_text(json.dumps({"session_result": {"segments_committed": 1},
                                   "live_terminal_state": {}}))

        res = _harness().qualify(root, cap)
        assert res["verdict"] == "NOT_QUALIFIED", (
            "zero markets compared is the definition of a vacuous pass")


class TestRecoveriesIsExcludedByContract:
    def test_the_harness_does_not_compare_recoveries(self):
        """P3 s8.2a: `recoveries` counts a collector ACTION, so the tape cannot
        carry it. Requiring equality would require the tape to contain
        something it is defined not to contain."""
        src = Path("scripts/kalshi_p4_replay_equality.py").read_text()
        compared_block = src[src.index('for k in ("snapshots"'):
                              src.index('"generation_boundaries")') + 30]
        assert "recoveries" not in compared_block
