"""KALSHI-DEMO-TRAFFIC-CAPACITY-001 — freeze the market pool.

Deterministic. Reads only the committed manifest artifact
`docs/experiments/KALSHI-TAPE-MANIFEST-001.json`; opens no socket, makes no
network call and needs no credential. Writes
`docs/experiments/KALSHI-DEMO-TRAFFIC-CAPACITY-001-POOL.json`.

The pool is frozen and committed BEFORE the probe connects, per the
preregistration §4. Re-running this script on the same artifact must produce a
byte-identical pool file; that is the whole point of it being a script rather
than a hand-written list.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MANIFEST = REPO / "docs/experiments/KALSHI-TAPE-MANIFEST-001.json"
OUT = REPO / "docs/experiments/KALSHI-DEMO-TRAFFIC-CAPACITY-001-POOL.json"

# KALSHI-TAPE-MANIFEST-001-AMENDMENT-TEST-INSTRUMENTS.md, approved 2026-08-15.
EXCLUDED_SERIES = ("KXMAXSHARDINGTEST", "KXTESTMATCH")

# The preregistration's two regimes, from KALSHI-TAPE-MANIFEST-001-FINDING §4.
HIGH_MAX_RANK = 4              # the four markets at >= 1,000 contracts/min
PLATEAU_LO, PLATEAU_HI = 15.0, 17.0    # the 15-17 c/min band (180 of 582)
PLATEAU_N = 8                  # 4 + 8 = 12, the qualification universe size

FIELDS = ("ticker", "event_ticker", "series", "rank", "statistic",
          "top_of_book_change_rate", "title", "close_time")


def build(manifest: dict) -> dict:
    eligible = manifest["candidate_population"]["eligible_ranked"]

    high = sorted((m for m in eligible if m["rank"] <= HIGH_MAX_RANK),
                  key=lambda m: m["rank"])
    if len(high) != HIGH_MAX_RANK:
        raise SystemExit(f"expected {HIGH_MAX_RANK} high-activity markets, "
                         f"found {len(high)}")

    band = sorted((m for m in eligible
                   if PLATEAU_LO <= m["statistic"] <= PLATEAU_HI
                   and m["series"] not in EXCLUDED_SERIES),
                  key=lambda m: m["rank"])

    # The manifest's own within_stratum_pick, reused verbatim so the plateau
    # sample is drawn the way the qualification manifest would have drawn it:
    # "Two deterministic passes in rank order: first accept only markets whose
    # event_ticker is unclaimed anywhere in the selection, then fill any
    # shortfall from the remainder."
    #
    # "anywhere in the selection" includes the high stratum, so the claimed set
    # is seeded with it: without the seed the plateau draw duplicated KXMLB-26,
    # which is already represented by the rank-1 market.
    claimed: set[str] = {m["event_ticker"] for m in high}
    picked: list[dict] = []
    for market in band:
        if len(picked) == PLATEAU_N:
            break
        if market["event_ticker"] not in claimed:
            picked.append(market)
            claimed.add(market["event_ticker"])
    if len(picked) < PLATEAU_N:
        chosen = {m["ticker"] for m in picked}
        for market in band:
            if len(picked) == PLATEAU_N:
                break
            if market["ticker"] not in chosen:
                picked.append(market)
    if len(picked) != PLATEAU_N:
        raise SystemExit(f"plateau band too small: {len(band)} candidates")

    row = lambda m: {k: m[k] for k in FIELDS}  # noqa: E731
    pool = {
        "pool_id": "KALSHI-DEMO-TRAFFIC-CAPACITY-001-POOL",
        "milestone": "KALSHI-DEMO-TRAFFIC-CAPACITY-001",
        "frozen_before_probe": True,
        "environment": "demo",
        "source_artifact": "docs/experiments/KALSHI-TAPE-MANIFEST-001.json",
        "source_artifact_sha256": hashlib.sha256(
            MANIFEST.read_bytes()).hexdigest(),
        "source_frame_digest_sha256":
            manifest["candidate_population"]["frame_digest_sha256"],
        "source_canonical_snapshot_timestamp":
            manifest["snapshot"]["canonical_snapshot_timestamp"],
        "excluded_series": list(EXCLUDED_SERIES),
        "exclusion_authority":
            "docs/experiments/KALSHI-TAPE-MANIFEST-001-AMENDMENT-TEST-INSTRUMENTS.md",
        "selection_rule": {
            "high": ("all four markets at rank 1-4 of the manifest's "
                     "eligible_ranked (the >= 1,000 contracts/min band). "
                     "No discretion: the preregistration names all four."),
            "plateau": (f"the {PLATEAU_LO}-{PLATEAU_HI} contracts/min band with "
                        "venue test series excluded, ordered by manifest rank "
                        "ascending, drawn with the manifest's own "
                        "within_stratum_pick (pass 1: first market of an "
                        "unclaimed event_ticker; pass 2: fill from the "
                        f"remainder), n={PLATEAU_N}"),
            "total": HIGH_MAX_RANK + PLATEAU_N,
            "no_post_hoc_optimisation": (
                "Frozen and committed before any socket was opened. A market "
                "that turns out quiet STAYS IN and its rate is reported."),
        },
        "plateau_band_candidates_after_test_exclusion": len(band),
        "high": [row(m) for m in high],
        "plateau": [row(m) for m in picked],
        "tickers": [m["ticker"] for m in high] + [m["ticker"] for m in picked],
    }
    return pool


def main() -> None:
    manifest = json.loads(MANIFEST.read_text())
    pool = build(manifest)
    OUT.write_text(json.dumps(pool, indent=2, sort_keys=True) + "\n")
    print(f"wrote {OUT.relative_to(REPO)}")
    for market in pool["high"] + pool["plateau"]:
        print(f"  rank {market['rank']:>4}  {market['ticker']:<48} "
              f"{market['statistic']:>10.2f} c/min  {market['event_ticker']}")


if __name__ == "__main__":
    main()
