"""KALSHI-DEMO-TRAFFIC-CAPACITY-001 — ticker lists for the doctrine-7 controls.

The frozen pool answers the question. These lists answer the prior question the
pool cannot: **when frames DO exist, does the counter move?** Doctrine 7 —
absence is not health, and a silent venue and a broken subscription produce
byte-identical zeroes.

Two control arms, both drawn from the SAME committed manifest artifact:

- `test-instruments` — `KXMAXSHARDINGTEST` / `KXTESTMATCH`. Venue load-test
  instruments, excluded from the qualification universe by the approved
  amendment, so they cannot contaminate the statistic no matter what they do.
  A sharding load test is the single most likely thing on a sandbox to be
  emitting messages, which makes it the strongest available positive control.
- `wide-eligible` — the top N eligible NON-test markets by manifest rank. If
  the twelve-market pool is silent but 200 markets are not, the pool is quiet;
  if 200 markets are also silent, the venue is.

Prints one comma-separated list. No network, no credential.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MANIFEST = REPO / "docs/experiments/KALSHI-TAPE-MANIFEST-001.json"
TEST_SERIES = ("KXMAXSHARDINGTEST", "KXTESTMATCH")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("arm", choices=("test-instruments", "wide-eligible"))
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--manifest", default=str(MANIFEST))
    args = parser.parse_args()

    eligible = json.loads(Path(args.manifest).read_text())[
        "candidate_population"]["eligible_ranked"]
    if args.arm == "test-instruments":
        chosen = [m for m in eligible if m["series"] in TEST_SERIES]
    else:
        chosen = [m for m in eligible if m["series"] not in TEST_SERIES]
    chosen.sort(key=lambda m: m["rank"])
    print(",".join(m["ticker"] for m in chosen[:args.limit]))


if __name__ == "__main__":
    main()
