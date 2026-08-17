"""KALSHI-CP7-LIVE-RERUN — freeze the market universe BEFORE any socket opens.

CP7 is re-run live to qualify `KALSHI-REPLAY-GENERATION-CONSISTENCY-001` on the
venue rather than on its captured frames. The universe must therefore be frozen
first, with its rule written down first, or the session's own telemetry becomes
available to the selection — which is the failure that killed the EDGE-SELECTION
lane.

**Read-only and credential-free.** One public `GET /trade-api/v2/markets` on
DEMO, the same route the tape manifest used. No socket, no key, no write.

**THE SELECTION RULE, stated before the query runs.**

1. The candidate population is every market the venue currently reports with
   `status=open` whose series prefix is `KXMAXSHARDINGTEST` or `KXTESTMATCH` —
   the two **venue test instrument** series named in
   `KALSHI-TAPE-MANIFEST-001-AMENDMENT-TEST-INSTRUMENTS.md`.
2. **Continuity first.** Any ticker from the 2026-08-17 CP7 session
   (`s2-reconnect-session.json`) that is still open is retained, in that
   session's original order. Re-running CP7 on the same universe is what makes
   the two results comparable.
3. **Top-up is telemetry-blind.** If fewer than 60 survive, the shortfall is
   filled from the remaining open test instruments sorted by **ticker
   ascending**. No volume, rate, liquidity, open-interest or top-of-book field
   is read by this script at all, so a market cannot enter the universe because
   it looks chatty. §1 of the preregistration: *no ticker may be replaced
   because its telemetry looks cleaner.*
4. The universe is capped at **60**, the prior session's size.
5. The **full candidate population** is recorded beside the selection, so the
   sampling frame is explicit and the 60 are never mistaken for a
   representative sample of the venue.

**These are venue test instruments and this is a FUNCTIONAL PROOF ONLY.** They
are used deliberately — 98.3% of the frames DEMO's eligible population emits
come from 194 of them, and a functional proof needs frames that exercise the
code paths. No rate, latency, throughput, capacity or microstructure-realism
claim may be derived from anything selected here (§8 rescope).
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

#: The venue's own test-instrument series. Named, not inferred from a substring
#: search, so a real market that merely contains "TEST" cannot be swept in.
TEST_SERIES = ("KXMAXSHARDINGTEST", "KXTESTMATCH")

PRIOR_SESSION = (REPO / "docs/experiments/KALSHI-CP6-CP9-FUNCTIONAL-RUNS"
                 / "s2-reconnect-session.json")

UNIVERSE_SIZE = 60

SCOPE_NOTE = (
    "VENUE TEST INSTRUMENTS. FUNCTIONAL PROOF ONLY. These markets are the "
    "venue's own test instruments (series KXMAXSHARDINGTEST / KXTESTMATCH). "
    "They are used deliberately because a functional proof needs frames that "
    "exercise the code paths, and they are worthless for microstructure. NO "
    "rate, latency-tail, throughput, capacity or microstructure-realism claim "
    "may be derived from this universe or from any session that uses it."
)


def series_of(ticker: str) -> str:
    """The series prefix. Kalshi tickers are `SERIES-EVENT-OUTCOME`."""
    return ticker.split("-", 1)[0]


def _get(client, params, *, pause: float, attempts: int = 6):
    """One GET, with backoff on the venue's rate limiter.

    DEMO returns 429 when the whole open universe is paginated, which is what
    this route did on the first attempt. Backing off is the correct response to
    a venue telling us we are asking too fast; retrying without one would just
    ask again.
    """
    delay = pause
    for attempt in range(attempts):
        response = client.get(DEMO_MARKETS_URL, params=params)
        if response.status_code == 429 and attempt < attempts - 1:
            time.sleep(delay)
            delay *= 2
            continue
        response.raise_for_status()
        return response.json()
    raise SystemExit("refused: the venue rate-limited every attempt; a "
                     "partially-paginated population is not a sampling frame")


def fetch_open_test_markets(timeout: float, *, pause: float = 1.0) -> list:
    """Every open market in the two test series. Public route, no credential.

    Queried **per series** rather than by paginating the whole venue. That is
    politer — DEMO rate-limited the full walk — and it is also the more exact
    statement of the population: the series are named in the rule, so they are
    what is asked for, instead of being filtered out of everything.
    """
    out = []
    with httpx.Client(timeout=timeout) as client:
        for series in TEST_SERIES:
            cursor, pages = None, 0
            while True:
                params = {"status": "open", "limit": 1000,
                          "series_ticker": series}
                if cursor:
                    params["cursor"] = cursor
                body = _get(client, params, pause=pause)
                pages += 1
                for market in body.get("markets") or []:
                    ticker = market.get("ticker")
                    if not ticker or series_of(ticker) != series:
                        continue
                    # ONLY identity fields are kept. Recording a volume or a
                    # rate here would make it available to a later rule, and a
                    # frozen universe that carries its own activity statistics
                    # is one amendment away from being a chosen one.
                    out.append({"ticker": ticker,
                                "event_ticker": market.get("event_ticker"),
                                "series": series,
                                "status": market.get("status"),
                                "close_time": market.get("close_time")})
                cursor = body.get("cursor")
                if not cursor:
                    break
                if pages > 200:
                    raise SystemExit(
                        f"refused: pagination did not terminate for {series}")
                time.sleep(pause)
    return out


def prior_universe() -> list:
    if not PRIOR_SESSION.exists():
        return []
    return list(json.loads(PRIOR_SESSION.read_text())["config"]["market_tickers"])


def build(candidates: list, prior: list, size: int) -> dict:
    open_tickers = [m["ticker"] for m in candidates]
    open_set = set(open_tickers)

    retained = [t for t in prior if t in open_set]
    dropped = [t for t in prior if t not in open_set]

    # Telemetry-blind top-up: ticker ascending, and nothing else.
    remaining = sorted(t for t in open_tickers if t not in set(retained))
    topped_up = remaining[: max(0, size - len(retained))]

    selected = (retained + topped_up)[:size]
    return {
        "milestone": "KALSHI-CP7-LIVE-RERUN",
        "scope_note": SCOPE_NOTE,
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "route": ("GET /trade-api/v2/markets?status=open&series_ticker=<series>"
                  " (public, no credential), one call per test series"),
        "selection_rule": {
            "test_series": list(TEST_SERIES),
            "continuity": "prior CP7 session tickers still open, in their "
                          "original order",
            "top_up": "remaining open test instruments, ticker ascending — "
                      "telemetry-blind",
            "universe_size": size,
            "prior_session_artifact": str(PRIOR_SESSION.relative_to(REPO)),
        },
        "candidate_population": {
            "open_test_instruments": len(candidates),
            "by_series": {s: sum(1 for m in candidates if m["series"] == s)
                          for s in TEST_SERIES},
            "markets": sorted(candidates, key=lambda m: m["ticker"]),
        },
        "prior_universe": {
            "size": len(prior),
            "still_open_and_retained": len(retained),
            "closed_since_and_dropped": len(dropped),
            "dropped_tickers": dropped,
        },
        "topped_up_count": len(topped_up),
        "topped_up_tickers": topped_up,
        "universe": selected,
        "universe_size": len(selected),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--size", type=int, default=UNIVERSE_SIZE)
    args = parser.parse_args()

    frozen = build(fetch_open_test_markets(args.timeout), prior_universe(),
                   args.size)
    if not frozen["universe"]:
        raise SystemExit("refused: the venue reports no open test instruments; "
                         "a session with no market measures nothing")
    Path(args.out).write_text(json.dumps(frozen, indent=2, sort_keys=True) + "\n")
    print(json.dumps({k: v for k, v in frozen.items()
                      if k not in ("candidate_population", "universe")},
                     indent=2, sort_keys=True))
    print(f"universe of {frozen['universe_size']} written to {args.out}")


if __name__ == "__main__":
    main()
