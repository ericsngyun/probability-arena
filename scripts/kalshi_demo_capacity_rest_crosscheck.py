"""KALSHI-DEMO-TRAFFIC-CAPACITY-001 — the REST cross-check.

Doctrine 8, applied to the OTHER side of the question. The websocket probe says
how many frames arrive. This says whether the same markets are moving at all
over the same interval, read through a completely different route — the public
`GET /markets` the tape manifest used, with no credential and no socket.

The point is to make silence interpretable. "Zero frames" has two very
different explanations:

- the markets are not moving, and the socket is correctly reporting that;
- the markets ARE moving and the socket is not telling us.

Those are indistinguishable from the socket alone, and they lead to opposite
conclusions about DEMO. So the same twelve markets are read twice, `--interval`
apart, and what actually changed is recorded field by field — lifetime volume
(the monotone counter the manifest settled on), top of book, resting sizes, and
`updated_time`, which is kept in the table specifically because the manifest
proved it does NOT track trading and its 0/N is the control on the other rows.

Read-only. One HTTP verb, `GET`. No credential is loaded, read or printed.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
DEMO_MARKETS_URL = "https://external-api.demo.kalshi.co/trade-api/v2/markets"

# NO field allowlist. Every key the venue returns is diffed, so the answer to
# "what moved" is discovered rather than assumed — the manifest's mistake was
# picking a field by its name before knowing what drove it. `updated_time` is
# therefore in the table by construction, and its row is the control on the
# others: the manifest proved it does not track trading.
def read(tickers, timeout: float) -> dict:
    """One batched GET for the whole pool.

    Per ticker was the obvious shape and it earned a 429 on the twelfth
    request. Batching is also the correct measurement: the two reads of a
    market are then separated by the interval and nothing else, instead of by
    the interval plus however long the loop took to reach that market.
    """
    out = {t: None for t in tickers}
    with httpx.Client(timeout=timeout) as client:
        for attempt in range(5):
            response = client.get(DEMO_MARKETS_URL,
                                  params={"tickers": ",".join(tickers),
                                          "limit": 1000})
            if response.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            response.raise_for_status()
            for market in response.json().get("markets") or []:
                if market.get("ticker") in out:
                    out[market["ticker"]] = market
            return out
    raise SystemExit("refused: the venue rate-limited every attempt; a "
                     "cross-check that could not read is not a cross-check "
                     "that found nothing")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pool", default=str(
        REPO / "docs/experiments/KALSHI-DEMO-TRAFFIC-CAPACITY-001-POOL.json"))
    parser.add_argument("--tickers", default="")
    parser.add_argument("--interval", type=float, default=600.0)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    tickers = ([t for t in args.tickers.split(",") if t] if args.tickers
               else json.loads(Path(args.pool).read_text())["tickers"])

    first_at = datetime.now(timezone.utc).isoformat()
    first = read(tickers, args.timeout)
    time.sleep(args.interval)
    second_at = datetime.now(timezone.utc).isoformat()
    second = read(tickers, args.timeout)

    moved: dict[str, int] = {}
    present = 0
    per_market = {}
    for ticker in tickers:
        a, b = first.get(ticker), second.get(ticker)
        if not isinstance(a, dict) or not isinstance(b, dict):
            per_market[ticker] = {"present_in_both_reads": False}
            continue
        present += 1
        fields = sorted(set(a) | set(b))
        changed = [f for f in fields if a.get(f) != b.get(f)]
        for field in fields:
            moved.setdefault(field, 0)
        for field in changed:
            moved[field] += 1
        per_market[ticker] = {
            "present_in_both_reads": True,
            "fields_that_moved": changed,
            "first": {f: a.get(f) for f in changed},
            "second": {f: b.get(f) for f in changed},
        }

    payload = {
        "milestone": "KALSHI-DEMO-TRAFFIC-CAPACITY-001",
        "route": "GET /trade-api/v2/markets (public, no credential)",
        "environment": "demo",
        "first_read_at": first_at,
        "second_read_at": second_at,
        "interval_seconds_requested": args.interval,
        "markets_requested": len(tickers),
        "markets_present_in_both_reads": present,
        "markets_that_moved_any_watched_field": sum(
            1 for v in per_market.values()
            if v.get("present_in_both_reads") and v["fields_that_moved"]),
        "fields_that_moved_count_of_markets": moved,
        "per_market": per_market,
    }
    Path(args.out).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps({k: payload[k] for k in (
        "first_read_at", "second_read_at", "markets_requested",
        "markets_present_in_both_reads",
        "markets_that_moved_any_watched_field",
        "fields_that_moved_count_of_markets")}, indent=2))


if __name__ == "__main__":
    main()
