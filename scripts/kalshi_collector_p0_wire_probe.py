"""KALSHI-COLLECTOR-P0-FIXES — the wire evidence, captured BEFORE anything is patched.

Doctrine 8: *a field name is not evidence of its semantics.* The three P0
defects were diagnosed from counters and from a list of KEY NAMES. This probe
answers the three questions the counters could not, from the venue itself:

1. **What does a `trade` frame actually look like?** Does it carry `seq`? Which
   `sid` — the orderbook subscription's, or its own?
2. **What is in the `error` body** that the collector's own `get_snapshot`
   recovery provokes? Nobody has read it.
3. **What is actually inside an `orderbook_snapshot`?** The prior probe recorded
   `msg_keys` only, so "no ladder" was inferred from a KEY LIST. A key list is
   not a value. This one records the values.

It is the capacity probe's transport tap (`kalshi_demo_capacity_probe.py`) with
the recorder replaced: instead of counting frames it keeps whole frame BODIES
for a bounded sample, plus a per-`sid` ordering census.

**Read-only, and the collector runs exactly as shipped.** Market-data channels
only, `dry_run=True` / `archive_root=None` so no archive is ever built and
`append()` is never called; the only outbound commands are the ones the
collector itself builds (`subscribe`, and the `get_snapshot` recovery whose
refusal is the thing being measured). No key material is printed: the credential
audit records a fingerprint and nothing else.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

from app.realtime.collector import (  # noqa: E402
    CollectorConfig,
    collect_once,
    load_observer_signer,
)
from app.realtime.credential_audit import audit_scopes  # noqa: E402
from app.realtime.auth import ReadOnlyRequestSigner  # noqa: E402
from app.realtime.kalshi import REST_HOSTS  # noqa: E402
from app.config import get_settings  # noqa: E402

ENVIRONMENT = "demo"


def _plain(value):
    """Venue values, JSON-safe and LOSSLESS.

    `loads_exact` turns every venue number into a `Decimal`, which `json.dumps`
    refuses. It is rendered with `str()`, never `float()` — the whole reason the
    transport parses exactly is that a float round-trip is not reversible, and a
    probe that undid that would be recording a different number from the one the
    venue sent.
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


class WireRecorder:
    """Whole frames, a per-sid ordering census, and the outbound commands.

    Nothing here interprets a frame beyond reading `type`, `sid` and `seq` off
    the top level. Every absence is recorded as an absence: a frame with no
    `seq` is counted under `seq_absent`, never defaulted to a number.
    """

    def __init__(self, *, samples_per_type: int):
        self.samples_per_type = samples_per_type
        self.total = 0
        self.by_type: Counter = Counter()
        self.samples: dict = {}                 # type -> [full frame, ...]
        self.errors: list = []                  # EVERY error frame, whole
        self.acks: list = []                    # EVERY subscribed ack, whole
        # sid -> census. `sid` is read off the TOP level, which is where the
        # ordering fields live; the ack's `msg.sid` is a different statement and
        # is kept separately in `acks`.
        self.sids: dict = {}
        self.commands: list = []
        # Ladder-bearing and ladder-less snapshots are sampled SEPARATELY, so a
        # run cannot answer "does a snapshot carry a ladder" with whichever kind
        # happened to arrive first.
        self.snapshots_with_ladder: list = []
        self.snapshots_without_ladder: list = []
        self.snapshot_ladder_census: Counter = Counter()

    def _census(self, sid, seq, event_type):
        entry = self.sids.get(sid)
        if entry is None:
            entry = self.sids[sid] = {
                "sid": sid, "frames": 0, "types": Counter(),
                "seq_absent": 0, "seq_first": None, "seq_last": None,
                "seq_contiguous": 0, "seq_gaps": 0, "seq_duplicates": 0,
                "seq_regressions": 0, "gap_examples": [],
            }
        entry["frames"] += 1
        entry["types"][event_type] += 1
        if seq is None:
            entry["seq_absent"] += 1
            return
        if entry["seq_first"] is None:
            entry["seq_first"] = seq
        else:
            previous = entry["seq_last"]
            if seq == previous + 1:
                entry["seq_contiguous"] += 1
            elif seq == previous:
                entry["seq_duplicates"] += 1
            elif seq < previous:
                entry["seq_regressions"] += 1
            else:
                entry["seq_gaps"] += 1
                if len(entry["gap_examples"]) < 8:
                    entry["gap_examples"].append(
                        {"previous": previous, "observed": seq,
                         "event_type": event_type})
        if seq is not None and (entry["seq_last"] is None or seq > entry["seq_last"]):
            entry["seq_last"] = seq

    def observe(self, message: object) -> None:
        self.total += 1
        if type(message) is not dict:
            self.by_type["__not_a_dict__"] += 1
            return
        raw_type = message.get("type")
        event_type = raw_type if type(raw_type) is str else "__no_type__"
        self.by_type[event_type] += 1

        sid = message.get("sid")
        seq = message.get("seq")
        sid_key = sid if isinstance(sid, int) and not isinstance(sid, bool) else "__no_sid__"
        seq_value = seq if isinstance(seq, int) and not isinstance(seq, bool) else None
        self._census(sid_key, seq_value, event_type)

        frame = {"ordinal": self.total,
                 "at": datetime.now(timezone.utc).isoformat(),
                 "frame": _plain(message)}

        bucket = self.samples.setdefault(event_type, [])
        if len(bucket) < self.samples_per_type:
            bucket.append(frame)
        if event_type == "error":
            self.errors.append(frame)
        elif event_type == "subscribed" and len(self.acks) < 64:
            self.acks.append(frame)
        elif event_type == "orderbook_snapshot":
            msg = message.get("msg")
            msg = msg if isinstance(msg, dict) else {}
            yes, no = msg.get("yes_dollars_fp"), msg.get("no_dollars_fp")
            # Four distinguishable states, and "key absent" is NOT merged with
            # "key present holding an empty list" — that distinction is the
            # whole question defect 2 asks.
            shape = (
                f"yes={'absent' if 'yes_dollars_fp' not in msg else ('empty' if not yes else f'{len(yes)}_levels')}"
                f" no={'absent' if 'no_dollars_fp' not in msg else ('empty' if not no else f'{len(no)}_levels')}")
            self.snapshot_ladder_census[shape] += 1
            laddered = bool(yes) or bool(no)
            target = (self.snapshots_with_ladder if laddered
                      else self.snapshots_without_ladder)
            if len(target) < self.samples_per_type:
                target.append({**frame, "ladder_shape": shape})

    def on_command(self, message) -> None:
        self.commands.append({
            "sent_after_frame_ordinal": self.total,
            "at": datetime.now(timezone.utc).isoformat(),
            "command": _plain(message)})

    def to_dict(self) -> dict:
        return {
            "frames_tapped": self.total,
            "by_type": dict(self.by_type),
            "per_sid_census": [
                {**entry, "types": dict(entry["types"])}
                for entry in sorted(self.sids.values(), key=lambda e: str(e["sid"]))],
            "commands_sent": self.commands,
            "error_frames_verbatim": self.errors,
            "subscribed_acks_verbatim": self.acks,
            "frame_samples_by_type": self.samples,
            "orderbook_snapshot_ladder_census": dict(self.snapshot_ladder_census),
            "orderbook_snapshots_with_ladder": self.snapshots_with_ladder,
            "orderbook_snapshots_without_ladder": self.snapshots_without_ladder,
        }


class TappedTransport:
    """Delegating wrapper — the collector runs unmodified underneath it.

    It cannot drop, reorder, retry or synthesise a frame, and it holds no
    credential: the signer lives inside the wrapped transport.
    """

    def __init__(self, inner, recorder: WireRecorder):
        self._inner = inner
        self._recorder = recorder

    @property
    def counters(self):
        return self._inner.counters

    @property
    def queue_depth(self):
        return self._inner.queue_depth

    @property
    def backpressure_active(self):
        return self._inner.backpressure_active

    @property
    def last_close(self):
        return self._inner.last_close

    @property
    def connected(self) -> bool:
        return self._inner.connected

    async def connect(self) -> None:
        await self._inner.connect()

    async def send(self, message) -> None:
        self._recorder.on_command(message)
        await self._inner.send(message)

    async def close(self) -> None:
        await self._inner.close()

    def __aiter__(self):
        return self._tap()

    async def _tap(self):
        async for message in self._inner:
            self._recorder.observe(message)
            yield message


def prove_read_only() -> tuple:
    """One GET to the key-metadata route. Halts unless scopes == ["read"]."""
    import httpx

    settings = get_settings()
    key_id = (settings.kalshi_observer_api_key_id or "").strip()
    credential_path = (settings.kalshi_observer_credential_path or "").strip()
    if not key_id or not credential_path:
        raise SystemExit(
            "refused: KALSHI_OBSERVER_API_KEY_ID and/or "
            "KALSHI_OBSERVER_CREDENTIAL_PATH are not set on this host")

    bootstrap = ReadOnlyRequestSigner.for_scope_audit(
        key_id=key_id, credential_path=credential_path, environment=ENVIRONMENT)
    base = REST_HOSTS[ENVIRONMENT].rsplit("/trade-api/v2", 1)[0]

    def fetch(path, headers):
        with httpx.Client(timeout=20.0) as client:
            response = client.get(base + path, headers=dict(headers))
            response.raise_for_status()
            return response.json()

    audit = audit_scopes(signer=bootstrap, key_id=key_id,
                         environment=ENVIRONMENT, fetch=fetch,
                         timestamp_ms=int(time.time() * 1000))
    signer = load_observer_signer(environment=ENVIRONMENT,
                                  reported_scopes=list(audit.scopes))
    return signer, {
        "environment": audit.environment,
        "key_id_fingerprint": audit.key_id_fingerprint,
        "scopes": list(audit.scopes),
        "proven_read_only": audit.proven_read_only,
        "verified_at": audit.verified_at,
        "detail": audit.detail,
    }


def run(*, tickers, channels, max_seconds, max_events, samples_per_type,
        validate_sequence, label, out_path, read_timeout_s, max_reconnects):
    signer, audit = prove_read_only()
    recorder = WireRecorder(samples_per_type=samples_per_type)

    from app.realtime.ws_transport import KalshiWebsocketTransport

    transports = []

    def transport_factory():
        inner = KalshiWebsocketTransport(environment=ENVIRONMENT, signer=signer,
                                         read_timeout_s=read_timeout_s)
        transports.append(inner)
        return TappedTransport(inner, recorder)

    config = CollectorConfig(
        environment=ENVIRONMENT,
        archive_root=None,
        market_tickers=tuple(tickers),
        channels=tuple(channels),
        max_seconds=max_seconds,
        max_events=max_events,
        max_reconnects=max_reconnects,
        dry_run=True,
        validate_sequence=validate_sequence,
    )
    started = datetime.now(timezone.utc).isoformat()
    result = collect_once(config, transport_factory=transport_factory)
    finished = datetime.now(timezone.utc).isoformat()

    payload = {
        "milestone": "KALSHI-COLLECTOR-P0-FIXES",
        "purpose": ("wire evidence for the three P0 defects, captured before "
                    "any collector change"),
        "run_label": label,
        "environment": ENVIRONMENT,
        "credential_audit": audit,
        "started_at": started,
        "finished_at": finished,
        "config": {
            "market_tickers_count": len(tickers),
            "market_tickers": list(tickers),
            "channels": list(channels),
            "max_seconds": max_seconds,
            "max_events": max_events,
            "dry_run": True,
            "validate_sequence": validate_sequence,
            "read_timeout_s": read_timeout_s,
            "max_reconnects": max_reconnects,
        },
        "session_result": result.to_dict(),
        "wire_counters_per_connection": [t.counters.snapshot() for t in transports],
        "wire": recorder.to_dict(),
    }
    Path(out_path).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "run_label": label,
        "status": result.status,
        "events_received": result.events_received,
        "sequence_faults": result.sequence_faults,
        "recoveries_requested": result.recoveries_requested,
        "by_type": dict(recorder.by_type),
        "sids": {str(e["sid"]): dict(e["types"]) for e in recorder.sids.values()},
        "snapshot_ladder_census": dict(recorder.snapshot_ladder_census),
        "error_frames": len(recorder.errors),
        "out": str(out_path),
    }, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tickers", default="")
    parser.add_argument("--tickers-file", default="")
    parser.add_argument("--channels", default="orderbook_delta,ticker,trade")
    parser.add_argument("--max-seconds", type=int, default=120)
    parser.add_argument("--max-events", type=int, default=20_000)
    parser.add_argument("--samples-per-type", type=int, default=8)
    parser.add_argument("--no-validate-sequence", action="store_true")
    parser.add_argument("--read-timeout-s", type=float, default=None)
    parser.add_argument("--max-reconnects", type=int, default=2)
    parser.add_argument("--label", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    if args.tickers_file:
        raw = Path(args.tickers_file).read_text()
        tickers = [t for t in (x.strip() for x in raw.split(",")) if t]
    else:
        tickers = [t for t in args.tickers.split(",") if t]
    if not tickers:
        raise SystemExit("refused: no market tickers; a session with no sample "
                         "frame measures nothing")

    run(tickers=tickers,
        channels=[c for c in args.channels.split(",") if c],
        max_seconds=args.max_seconds,
        max_events=args.max_events,
        samples_per_type=args.samples_per_type,
        validate_sequence=not args.no_validate_sequence,
        label=args.label,
        out_path=args.out,
        read_timeout_s=(args.read_timeout_s if args.read_timeout_s is not None
                        else float(args.max_seconds + 120)),
        max_reconnects=args.max_reconnects)


if __name__ == "__main__":
    main()
