"""KALSHI-COLLECTOR-P0-FIXES — is `get_snapshot` a real recovery, or no recovery at all?

The main wire probe established that the collector's `get_snapshot` recovery,
sent against the **trade** subscription, is answered

    {"type":"error","sid":3,"seq":2,"msg":{"code":13,"msg":"Unsupported action"}}

That leaves one question the fix depends on, and it is not a detail. Either

* `get_snapshot` is valid but was sent to the WRONG SUBSCRIPTION — a routing
  defect, and the recovery path itself is sound; or
* `get_snapshot` is unsupported on **every** subscription — in which case the
  collector's only recovery path does not exist, which is a far larger finding
  than the three P0 defects and must not be discovered during CP7.

Only the venue can tell them apart, so this probe asks it: subscribe to
`orderbook_delta` alone, wait for a real `orderbook_snapshot`, send exactly ONE
`get_snapshot` on the ORDERBOOK sid, and record verbatim what comes back.

**Read-only.** One market-data channel; the only two commands that reach the
socket are `subscribe` and the `get_snapshot` the collector itself would send,
both built by `kalshi.build_*` and re-validated by the transport's
`assert_sendable`. Nothing is archived, no database session is opened, and no
order, position, wallet or key-management surface is reachable from this file.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

from app.config import get_settings  # noqa: E402
from app.realtime.auth import ReadOnlyRequestSigner  # noqa: E402
from app.realtime.collector import load_observer_signer  # noqa: E402
from app.realtime.credential_audit import audit_scopes  # noqa: E402
from app.realtime.kalshi import (  # noqa: E402
    REST_HOSTS,
    build_get_snapshot,
    build_subscribe,
)

ENVIRONMENT = "demo"


def _plain(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def prove_read_only():
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
    return signer, {"environment": audit.environment,
                    "key_id_fingerprint": audit.key_id_fingerprint,
                    "scopes": list(audit.scopes),
                    "proven_read_only": audit.proven_read_only,
                    "verified_at": audit.verified_at}


async def probe(*, tickers, settle_s, observe_s, out_path):
    from app.realtime.ws_transport import KalshiWebsocketTransport

    signer, audit = prove_read_only()
    transport = KalshiWebsocketTransport(environment=ENVIRONMENT, signer=signer,
                                         read_timeout_s=float(settle_s + observe_s + 30))
    frames_before: list = []
    frames_after: list = []
    orderbook_sid = None
    sent_at_ordinal = None
    command_sent = None

    await transport.connect()
    await transport.send(build_subscribe(1, ["orderbook_delta"], list(tickers)))
    started = time.monotonic()
    ordinal = 0
    deadline_send = started + settle_s
    deadline_end = started + settle_s + observe_s

    async for message in transport:
        ordinal += 1
        record = {"ordinal": ordinal,
                  "at": datetime.now(timezone.utc).isoformat(),
                  "frame": _plain(message)}
        # The venue's own sid<->channel statement, from the ack.
        if message.get("type") == "subscribed":
            msg = message.get("msg") or {}
            if msg.get("channel") == "orderbook_delta":
                orderbook_sid = msg.get("sid")
        if command_sent is None:
            if len(frames_before) < 40:
                frames_before.append(record)
        elif len(frames_after) < 40:
            frames_after.append(record)

        now = time.monotonic()
        if (command_sent is None and orderbook_sid is not None
                and now >= deadline_send):
            # EXACTLY ONE recovery command, on the ORDERBOOK subscription, built
            # by the same builder the collector uses. This is the measurement.
            command = build_get_snapshot(2, int(orderbook_sid), list(tickers))
            command_sent = _plain(command)
            sent_at_ordinal = ordinal
            await transport.send(command)
        if now >= deadline_end:
            break

    await transport.close()

    payload = {
        "milestone": "KALSHI-COLLECTOR-P0-FIXES",
        "question": ("is get_snapshot valid on the ORDERBOOK subscription, or "
                     "unsupported on every subscription?"),
        "environment": ENVIRONMENT,
        "credential_audit": audit,
        "market_tickers": list(tickers),
        "orderbook_sid_from_ack": orderbook_sid,
        "command_sent": command_sent,
        "command_sent_after_frame_ordinal": sent_at_ordinal,
        "frames_before_command": frames_before,
        "frames_after_command": frames_after,
        "wire_counters": transport.counters.snapshot(),
    }
    Path(out_path).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    after_types = {}
    for record in frames_after:
        t = record["frame"].get("type")
        after_types[t] = after_types.get(t, 0) + 1
    print(json.dumps({
        "orderbook_sid": orderbook_sid,
        "frames_after_command_types": after_types,
        "errors_after": [r["frame"] for r in frames_after
                         if r["frame"].get("type") == "error"],
        "out": str(out_path),
    }, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tickers", required=True)
    parser.add_argument("--settle-seconds", type=float, default=8.0)
    parser.add_argument("--observe-seconds", type=float, default=12.0)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    tickers = [t for t in args.tickers.split(",") if t]
    if not tickers:
        raise SystemExit("refused: no market tickers")
    asyncio.run(probe(tickers=tickers, settle_s=args.settle_seconds,
                      observe_s=args.observe_seconds, out_path=args.out))


if __name__ == "__main__":
    main()
