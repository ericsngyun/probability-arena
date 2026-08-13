"""KALSHI-ARCHIVE-CORE-REMEDIATION-003B A5 -- the residue decompression
amplifier.

WHY THIS EXISTS. `evidence_fs.bounded_read` caps the COMPRESSED bytes read
from disk (`DEFAULT_MAX_READ_BYTES`, 1 GiB) before `segment.
read_segment_records` ever decompresses them -- but `segment.
_decompress_prefix` then calls `zlib.decompressobj(31).decompress(chunk)`
with NO `max_length` argument, on every 64 KiB chunk of however many bytes
`bounded_read` handed it. `zlib.decompressobj.decompress` with no
`max_length` decompresses AS FAR AS THE INPUT ALLOWS, unbounded in its
OUTPUT size regardless of how small the compressed input was. A handful of
kilobytes of a highly-compressible payload (e.g. long runs of a repeated
byte, or a compressed JSONL stream of repeated records) can therefore
expand to gigabytes in RAM, entirely within the "1 GiB compressed input"
bound `bounded_read` enforces.

REACHABLE BY ANYONE WHO CAN `mkdir` A SEGMENT DIRECTORY: `read_segment_
records` is called by `verify_segment(allow_open=True)`, which
`verify_archive`'s own residue-inspection loop calls on EVERY uncommitted
segment directory it discovers under the environment root (see
`segment.py`'s `uncommitted_detail` loop) -- no manifest, no ownership
lock, and no prior admission gate stands between an attacker who can create
`env=<env>/segment=<id>/events.jsonl.gz` and a `verify_archive` call that
will decompress whatever is inside it, in-process, with no output cap.

THIS MODULE builds such a payload and provides an OUT-OF-PROCESS harness
(via `tests/meta_runtime/parent_timeout.run_with_parent_timeout`) to
measure decompressed RSS and wall time without ever risking the calling
test process's own memory or the suite's ability to regain control.
"""

from __future__ import annotations

import gzip

# Small compressed footprint, deliberately far below any reasonable
# "this is obviously an attack" heuristic based on file size alone.
_REPEATED_LINE = (
    b'{"schema_version":1,"canonical_schema_version":1,"environment":"demo",'
    b'"segment_id":"bomb","connection_generation":1,"subscription_id":1,'
    b'"subscription_generation":1,"receive_ordinal":0,'
    b'"message_type":"orderbook_delta","market_ticker":"KXA","seq":0,'
    b'"received_at_utc":"2026-08-10T12:00:00.000000Z",'
    b'"received_monotonic_ns":1,"raw_event":{},"normalized_event":{},'
    b'"previous_record_digest":"genesis:x","record_digest":"x"}\n'
)


def make_decompression_bomb(target_decompressed_bytes: int) -> bytes:
    """Highly-compressible gzip payload that expands to approximately
    `target_decompressed_bytes` when decompressed -- typically a
    three-to-four-order-of-magnitude compression ratio for repeated JSONL
    lines, which is realistic for what an attacker could construct (not an
    artificially extreme all-zeros ratio)."""
    n_repeats = max(target_decompressed_bytes // len(_REPEATED_LINE), 1)
    raw = _REPEATED_LINE * n_repeats
    return gzip.compress(raw, compresslevel=9)


def build_residue_segment(root, environment: str, segment_id: str,
                          target_decompressed_bytes: int) -> tuple:
    """Writes ONLY the events file -- no manifest, no writer lock, no
    ownership -- exactly what `mkdir` + a dropped file gives an attacker.
    Returns (segment_dir, compressed_bytes_on_disk)."""
    from pathlib import Path

    seg_dir = Path(root) / f"env={environment}" / f"segment={segment_id}"
    seg_dir.mkdir(parents=True, exist_ok=True)
    payload = make_decompression_bomb(target_decompressed_bytes)
    (seg_dir / "events.jsonl.gz").write_bytes(payload)
    return seg_dir, len(payload)


MEASUREMENT_SCRIPT_TEMPLATE = '''
import sys
sys.path.insert(0, {repo_root!r})
import json
import resource
import time
from app.realtime import segment as sg

t0 = time.monotonic()
records = sg.read_segment_records({events_path!r})
elapsed = time.monotonic() - t0
rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
print(json.dumps({{
    "elapsed_s": elapsed,
    "ru_maxrss": rss,
    "n_records_read": len(records),
}}))
'''


def measurement_script(repo_root: str, events_path: str) -> str:
    return MEASUREMENT_SCRIPT_TEMPLATE.format(
        repo_root=repo_root, events_path=events_path)
