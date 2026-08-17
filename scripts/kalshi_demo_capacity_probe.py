"""KALSHI-DEMO-TRAFFIC-CAPACITY-001 — direct measurement of frame arrival.

**This is NOT CP6.** It borrows CP6's `--dry-run` mechanism — real socket, real
signed handshake, real subscription, real venue frames, `archive_root=None` so
`_Session._archive` stays `None` and `append()` is never called — to answer one
capacity question. It archives nothing, claims no checkpoint, and stops as soon
as the question is answered.

**Read-only.** The only channels it will subscribe to are market-data channels
from `kalshi.ALLOWED_CHANNELS`; `CollectorConfig.__post_init__` runs
`assert_channels_allowed` at construction, so a portfolio channel is a
`CapabilityError` before any object exists that could open a socket. The
credential is loaded by `app.realtime.auth` through the ordinary confined path
and its scopes are proven `["read"]` against the venue BEFORE the websocket
signer is built. Nothing here prints, copies or logs key material: the audit
records a fingerprint, and every refusal names variables, never values.

**Why a transport tap rather than a patched collector.** `run_session` already
takes a `transport_factory` seam. The tap wraps the real
`KalshiWebsocketTransport` and counts each frame on its way THROUGH to the
collector, so the collector runs entirely unmodified and the tap's count can be
cross-checked against three counters it does not own (the transport's
`frames_received`/`frames_yielded` and the session's `events_received`). Four
independently maintained counters agreeing is the empirical answer to "what
moves my counter" that doctrine 8 asks for.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from datetime import datetime, timezone
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


# =============================================================================
# The frame recorder
# =============================================================================

class FrameRecorder:
    """Counts frames by (observation bin, market ticker, venue event type).

    Nothing here interprets a frame. It reads two keys — the top-level `type`
    and `msg.market_ticker` — and both are recorded as OBSERVED, including when
    they are absent: a frame that carries no ticker is bucketed under
    `__no_market_ticker__` rather than being dropped or attributed to a guess.
    Attribution by assumption is exactly what this milestone exists to avoid.
    """

    def __init__(self, *, bin_seconds: float, semantics_sample: int):
        self.bin_ns = int(bin_seconds * 1e9)
        self.bin_seconds = bin_seconds
        self.semantics_sample = semantics_sample
        self.t0_ns: int | None = None
        self.t0_wall: str | None = None
        self.counts: Counter = Counter()          # (bin, ticker, type) -> n
        self.type_counts: Counter = Counter()
        self.ticker_counts: Counter = Counter()
        self.total = 0
        self.non_dict = 0
        self.shapes: list[dict] = []
        self.last_ns: int | None = None
        self.connections = 0

    def on_connection(self) -> None:
        self.connections += 1

    def observe(self, message: object) -> None:
        now = time.monotonic_ns()
        if self.t0_ns is None:
            self.t0_ns = now
            self.t0_wall = datetime.now(timezone.utc).isoformat()
        self.last_ns = now
        self.total += 1

        if type(message) is not dict:
            self.non_dict += 1
            event_type, ticker = "__not_a_dict__", "__no_market_ticker__"
        else:
            raw_type = message.get("type")
            event_type = raw_type if type(raw_type) is str else "__no_type__"
            msg = message.get("msg")
            raw_ticker = msg.get("market_ticker") if isinstance(msg, dict) else None
            ticker = raw_ticker if type(raw_ticker) is str else "__no_market_ticker__"

        index = (now - self.t0_ns) // self.bin_ns
        self.counts[(index, ticker, event_type)] += 1
        self.type_counts[event_type] += 1
        self.ticker_counts[ticker] += 1

        if len(self.shapes) < self.semantics_sample and type(message) is dict:
            msg = message.get("msg")
            self.shapes.append({
                "ordinal": self.total,
                "type": event_type,
                "top_level_keys": sorted(str(k) for k in message),
                "msg_keys": (sorted(str(k) for k in msg)
                             if isinstance(msg, dict) else None),
                "market_ticker": (ticker if ticker != "__no_market_ticker__"
                                  else None),
                "seconds_since_first_frame": round((now - self.t0_ns) / 1e9, 4),
            })

    # -- reporting ------------------------------------------------------------

    def span_seconds(self) -> float:
        if self.t0_ns is None or self.last_ns is None:
            return 0.0
        return (self.last_ns - self.t0_ns) / 1e9

    def bin_table(self) -> list[dict]:
        """One row per observation bin: total, and the per-ticker breakdown.

        The LAST bin is emitted with `complete=False` — it was cut short by the
        session cap, so its count is not a full bin's worth and must not enter a
        rate estimate as though it were.
        """
        if not self.counts:
            return []
        max_index = max(index for index, _, _ in self.counts)
        per_bin: dict[int, Counter] = {i: Counter() for i in range(max_index + 1)}
        per_bin_type: dict[int, Counter] = {i: Counter() for i in range(max_index + 1)}
        for (index, ticker, event_type), n in self.counts.items():
            per_bin[index][ticker] += n
            per_bin_type[index][event_type] += n
        rows = []
        for index in range(max_index + 1):
            rows.append({
                "bin": index,
                "bin_start_seconds": round(index * self.bin_seconds, 3),
                "complete": index < max_index,
                "total": sum(per_bin[index].values()),
                "by_ticker": dict(per_bin[index]),
                "by_type": dict(per_bin_type[index]),
            })
        return rows


class TappedTransport:
    """Delegating wrapper. Counts frames on their way to the collector.

    Everything the collector uses is delegated verbatim, including `counters`
    (the collector reads `bytes_received` off it per frame) and `queue_depth` /
    `backpressure_active` (its overload gauges). The wrapper adds no behaviour:
    it cannot drop, reorder, retry or synthesise a frame, and it holds no
    credential — the signer lives inside the wrapped transport.
    """

    def __init__(self, inner, recorder: FrameRecorder):
        self._inner = inner
        self._recorder = recorder

    # -- delegated surface ----------------------------------------------------
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
        self._recorder.on_connection()

    async def send(self, message) -> None:
        await self._inner.send(message)

    async def close(self) -> None:
        await self._inner.close()

    def __aiter__(self):
        return self._tap()

    async def _tap(self):
        async for message in self._inner:
            self._recorder.observe(message)
            yield message


# =============================================================================
# Credential — audited before the socket, never printed
# =============================================================================

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


# =============================================================================
# The run
# =============================================================================

def run(*, tickers, channels, max_seconds, max_events, bin_seconds,
        semantics_sample, validate_sequence, label, out_path,
        read_timeout_s, max_reconnects):
    signer, audit = prove_read_only()
    recorder = FrameRecorder(bin_seconds=bin_seconds,
                             semantics_sample=semantics_sample)

    from app.realtime.ws_transport import KalshiWebsocketTransport

    transports = []

    def transport_factory():
        # `read_timeout_s` is raised above the session length ON PURPOSE for the
        # measurement runs. At the shipped 60 s default, a quiet pool trips
        # `TransportReadTimeout`, the collector reconnects, and the resubscribe
        # replays one `orderbook_snapshot` PER MARKET — so an artefact of the
        # instrument would appear in the data as periodic bursts of frames, and
        # a silent venue would be measured as a mildly active one. Silence has
        # to stay silent for the count to mean anything. The transport's own
        # `closes` / `bytes_received` counters are reported so a genuinely dead
        # socket is still distinguishable from a live quiet one.
        inner = KalshiWebsocketTransport(environment=ENVIRONMENT, signer=signer,
                                         read_timeout_s=read_timeout_s)
        transports.append(inner)
        return TappedTransport(inner, recorder)

    config = CollectorConfig(
        environment=ENVIRONMENT,
        archive_root=None,          # dry-run: the session never builds an archive
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

    wire = [t.counters.snapshot() for t in transports]
    payload = {
        "milestone": "KALSHI-DEMO-TRAFFIC-CAPACITY-001",
        "run_label": label,
        "not_cp6": ("borrows CP6's dry-run mechanism; archives nothing and "
                    "claims no checkpoint"),
        "environment": ENVIRONMENT,
        "credential_audit": audit,
        "started_at": started,
        "finished_at": finished,
        "config": {
            "market_tickers": list(tickers),
            "channels": list(channels),
            "max_seconds": max_seconds,
            "max_events": max_events,
            "dry_run": True,
            "validate_sequence": validate_sequence,
            "bin_seconds": bin_seconds,
            "read_timeout_s": read_timeout_s,
            "max_reconnects": max_reconnects,
        },
        "session_result": result.to_dict(),
        "wire_counters_per_connection": wire,
        "recorder": {
            "frames_tapped": recorder.total,
            "frames_not_a_dict": recorder.non_dict,
            "first_frame_at": recorder.t0_wall,
            "span_seconds": round(recorder.span_seconds(), 4),
            "connections": recorder.connections,
            "by_type": dict(recorder.type_counts),
            "by_ticker": dict(recorder.ticker_counts),
        },
        "frame_shapes_observed": recorder.shapes,
        "bins": recorder.bin_table(),
    }
    Path(out_path).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "run_label": label,
        "status": result.status,
        "events_received": result.events_received,
        "events_archived": result.events_archived,
        "frames_malformed": result.frames_malformed,
        "frames_tapped": recorder.total,
        "span_seconds": round(recorder.span_seconds(), 2),
        "reconnects": result.reconnects,
        "by_type": dict(recorder.type_counts),
        "distinct_tickers": len(recorder.ticker_counts),
        "out": str(out_path),
    }, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool", default=str(
        REPO / "docs/experiments/KALSHI-DEMO-TRAFFIC-CAPACITY-001-POOL.json"))
    parser.add_argument("--tickers", default="",
                        help="comma-separated override, for control runs only")
    parser.add_argument("--tickers-file", default="",
                        help="file holding a comma-separated override, for "
                             "control runs only")
    parser.add_argument("--channels", default="orderbook_delta,ticker,trade")
    parser.add_argument("--max-seconds", type=int, default=900)
    parser.add_argument("--max-events", type=int, default=5_000_000)
    parser.add_argument("--bin-seconds", type=float, default=5.0)
    parser.add_argument("--semantics-sample", type=int, default=40)
    parser.add_argument("--no-validate-sequence", action="store_true")
    parser.add_argument("--read-timeout-s", type=float, default=None,
                        help="default: max_seconds + 120, so silence stays "
                             "silent instead of becoming a reconnect burst")
    parser.add_argument("--max-reconnects", type=int, default=5)
    parser.add_argument("--label", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    if args.tickers_file:
        raw = Path(args.tickers_file).read_text()
        tickers = [t for t in (x.strip() for x in raw.split(",")) if t]
    elif args.tickers:
        tickers = [t for t in args.tickers.split(",") if t]
    else:
        tickers = json.loads(Path(args.pool).read_text())["tickers"]

    run(tickers=tickers,
        channels=[c for c in args.channels.split(",") if c],
        max_seconds=args.max_seconds,
        max_events=args.max_events,
        bin_seconds=args.bin_seconds,
        semantics_sample=args.semantics_sample,
        validate_sequence=not args.no_validate_sequence,
        label=args.label,
        out_path=args.out,
        read_timeout_s=(args.read_timeout_s if args.read_timeout_s is not None
                        else float(args.max_seconds + 120)),
        max_reconnects=args.max_reconnects)


if __name__ == "__main__":
    main()
