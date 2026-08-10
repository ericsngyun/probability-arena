"""Build a real, healthy KALSHI archive on disk for the totality matrix.

Everything here goes through the PRODUCTION write path
(`initialize_archive`, `EventArchive.append`, `EventArchive.close`) so the
"known-good" baseline is exactly what a real collector would produce -- not a
hand-rolled JSON blob that merely looks right.

Roots are kept SHORT (`/tmp/kfst-<token>`), not under pytest's `tmp_path`,
because one of the required shapes is a real AF_UNIX socket bound at the
artifact path, and `sun_path` is capped at roughly 104-108 bytes -- pytest's
default tmp dirs (`/private/var/folders/.../pytest-of-.../test_name0/...`)
blow through that on macOS.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

SHORT_ROOT_PREFIX = "/tmp/kfst-"


def make_short_root() -> Path:
    token = uuid.uuid4().hex[:10]
    root = Path(f"{SHORT_ROOT_PREFIX}{token}")
    root.mkdir(parents=True, exist_ok=False)
    return root


def teardown_root(root: Path) -> None:
    from tests.harness_filesystem_totality.shapes import cleanup_permissions
    if not root.exists():
        return
    cleanup_permissions(root)
    shutil.rmtree(root, ignore_errors=True)


NOW = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)


def _make_envelope(seq: int, environment: str):
    from app.realtime import book as bk
    return bk.make_envelope(
        venue="kalshi", environment=environment, channel="orderbook_delta",
        message={"type": "orderbook_delta", "sid": 1, "seq": seq,
                 "msg": {"market_ticker": "KXTEST", "market_id": "mid-1",
                        "yes_dollars_fp": [], "no_dollars_fp": []}},
        receive_time=NOW, receive_mono=bk.monotonic_ns())


def build_healthy_archive(root: Path, *, environment: str = "demo",
                          records: int = 3) -> dict:
    """A genuinely valid archive: genesis, one committed segment, current head.

    Returns a dict of the canonical artifact paths so the matrix can target
    each one precisely, plus `archive_id` for callers that need it.
    """
    from app.realtime import archive as ar
    from app.realtime import archive_head as ah

    genesis = ah.initialize_archive(
        root, environment, archive_identity="kalshi-fs-totality-harness")
    archive = ar.EventArchive(root, environment=environment,
                              expected_archive_id=genesis["archive_id"])
    for i in range(records):
        archive.append(_make_envelope(i, environment))
    manifests = archive.close()
    assert manifests, "fixture did not commit a segment"
    (segment_id,) = manifests.keys()

    env_dir = root / f"env={environment}"
    heads_dir = env_dir / "heads"
    segment_dir = env_dir / f"segment={segment_id}"
    return {
        "root": root,
        "environment": environment,
        "archive_id": genesis["archive_id"],
        "env_dir": env_dir,
        "genesis_path": env_dir / "archive-genesis.json",
        "current_head_path": env_dir / "archive-head.json",
        "heads_dir": heads_dir,
        "generation_0_path": heads_dir / "0000000000000000.json",
        "generation_1_path": heads_dir / "0000000000000001.json",
        "segment_id": segment_id,
        "segment_dir": segment_dir,
        "events_path": segment_dir / "events.jsonl.gz",
        "manifest_path": segment_dir / "manifest.json",
    }


def build_legacy_source(root: Path, *, records: int = 3) -> dict:
    """A pre-genesis legacy source tree: one gzipped jsonl file, no head."""
    import gzip
    import json

    events_dir = root / "legacy-source" / "kalshi" / "2026-08-09" / "12"
    events_dir.mkdir(parents=True, exist_ok=True)
    events_path = events_dir / "events.jsonl.gz"
    lines = []
    for i in range(records):
        lines.append(json.dumps({
            "environment": "demo", "event_type": "orderbook_delta", "sid": 1,
            "seq": i, "market_ticker": "KXTEST",
            "collector_receive_time": NOW.isoformat(),
            "receive_monotonic_ns": i, "raw": {"seq": i},
        }))
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    events_path.write_bytes(gzip.compress(payload))
    return {"source_dir": events_dir.parent.parent, "events_path": events_path}
