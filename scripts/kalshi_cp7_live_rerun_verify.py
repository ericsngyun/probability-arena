"""KALSHI-CP7-LIVE-RERUN — assert CP7's three properties on the LIVE artifacts.

Offline and pure: it reads the session JSON the live probe wrote and opens no
socket. It exists so the verdict is a re-runnable computation over committed
evidence rather than a paragraph someone wrote after reading a log.

**The three preregistered properties, plus the one CP7 could not settle.**

1. `generation_after > generation_before` at each forced boundary — with the
   unperturbed session as the paired control, so the counter is not pinned high.

2. **Per market, independently:** `old book -> nonpublishable -> ITS OWN
   new-generation snapshot -> publishable`. Asserted on the **SHAPE OF THE
   TRANSITION LOG**, never on an aggregate count. The failure signature to
   reject is CP7's: one transition entry carrying an acquisition for every
   market at once. The passing shape is N entries of one acquisition each, each
   caused by an `orderbook_snapshot` naming that same market.

3. **Anti-vacuity on live data:** a genuine WITHIN-generation gap still faults,
   still unpublishes, and is reported as `book_halted` — not as the benign
   `awaiting_snapshot_for_generation`. The typed state introduced by the fix
   must not have become a place for real faults to hide.

4. **The delta-refusal path**, reported rather than asserted. A new-generation
   delta landing on an un-re-snapshotted book must be REFUSED
   (`rejected_pre_generation_snapshot`). CP7 could only report that this "did
   not happen to occur", because the venue happened to send all 60 snapshots
   before any delta — which is not a contract we hold. This script says plainly
   which of the two happened, and never presents favourable ordering as proof.

**Scope.** The universe is 60 VENUE TEST INSTRUMENTS and this is a FUNCTIONAL
PROOF ONLY. No rate, latency-tail, throughput, capacity or microstructure-
realism claim may be derived from any number printed here (§8 rescope).
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RUNS = REPO / "docs/experiments/KALSHI-CP7-LIVE-RERUN-RUNS"

PUB = "publishable"
HALTED = "book_halted"
AWAITING = "awaiting_snapshot_for_generation"
UNHEALTHY = "subscription_unhealthy"

SCOPE_NOTE = (
    "VENUE TEST INSTRUMENTS, FUNCTIONAL PROOF ONLY. The 60 markets are the "
    "venue's own test instruments (KXMAXSHARDINGTEST / KXTESTMATCH), used "
    "deliberately because a functional proof needs frames that exercise the "
    "code paths. NO rate, latency-tail, throughput, capacity or "
    "microstructure-realism claim may be derived from anything here."
)


class Failed(AssertionError):
    """A property did not hold on the live evidence."""


def load(name: str) -> dict:
    return json.loads((RUNS / name).read_text())


# ---------------------------------------------------------------------------
# the transition log, read as a shape
# ---------------------------------------------------------------------------

def acquisitions(timeline: list, *, epoch: int) -> list:
    """Every transition INTO publishable, with the shape of its own entry.

    `acquisitions_in_this_entry` is the CP7 signature. It counts acquisitions
    rather than every typed change, because the frame carrying a new
    generation's first snapshot legitimately changes all 60 books — it moves
    the 59 siblings into `awaiting_snapshot_for_generation`, which is the fix
    WORKING. Counting those as violations would fail on correct behaviour.
    """
    out = []
    for entry in timeline:
        if entry["subscription_epoch"] != epoch:
            continue
        gained = [c for c in entry["changes"] if c["to"] is True]
        for change in gained:
            # `to_state` is absent in the 2026-08-17 artifacts, which carried
            # only the boolean. Read defensively so this function can be
            # pointed AT that failed run as a control — the shape check below
            # is what must fire there, and it must fire on the defect rather
            # than on a missing field (doctrine 10: absent is not a value).
            to_state = change.get("to_state") or {}
            out.append({
                "market": change["market_ticker"],
                "caused_by": entry["cause"]["market_ticker"],
                "cause_type": entry["cause"]["event_type"],
                "cause_seq": entry["cause"]["seq"],
                "frame_ordinal": entry["frame_ordinal"],
                "acquisitions_in_this_entry": len(gained),
                "based_generation": to_state.get("based_generation"),
                "subscription_generation":
                    to_state.get("subscription_generation"),
                "typed_state_recorded": bool(to_state),
            })
    return out


def check(condition, message, evidence=None):
    if not condition:
        raise Failed(f"{message}\n    evidence: {evidence!r}")


# ---------------------------------------------------------------------------
# PROPERTY 1
# ---------------------------------------------------------------------------

def property_1(reconnect: dict, control: dict) -> dict:
    """`generation_after > generation_before` at each boundary."""
    forced = [j for j in reconnect["perturbation_journal"]
              if j["event"] == "forced_socket_close"]
    check(forced, "the tap never fired — no boundary was forced", forced)

    # Epoch as OBSERVED across the timeline, boundary by boundary, rather than
    # only as a final counter: a final 3 could be reached any number of ways.
    seen = []
    for entry in reconnect["publishability_timeline"]:
        if not seen or entry["subscription_epoch"] != seen[-1]:
            seen.append(entry["subscription_epoch"])
    boundaries = [{"before": a, "after": b} for a, b in zip(seen, seen[1:])]
    for b in boundaries:
        check(b["after"] > b["before"],
              "a boundary did not advance the generation", b)
    check(len(boundaries) == len(forced),
          "the number of observed boundaries does not match the number of "
          "forced closes", {"forced": len(forced), "observed": len(boundaries)})

    metrics = reconnect["metrics"]
    check(metrics["reconnects"] == len(forced),
          "the metrics lane disagrees with the perturbation journal",
          {"metrics": metrics["reconnects"], "forced": len(forced)})

    # THE PAIRED CONTROL. A counter that reads 3 whatever happens measures
    # nothing, so the unperturbed session must end at 1 with zero reconnects.
    check(control["subscription_epoch_final"] == 1,
          "the unperturbed control advanced its epoch; the counter is not "
          "measuring reconnects", control["subscription_epoch_final"])
    check(control["metrics"]["reconnects"] == 0,
          "the unperturbed control counted a reconnect",
          control["metrics"]["reconnects"])

    return {
        "verdict": "PROVEN",
        "forced_closes": len(forced),
        "epoch_sequence_observed": seen,
        "boundaries": boundaries,
        "subscription_epoch_final": reconnect["subscription_epoch_final"],
        "connection_generation_final": reconnect["connection_generation_final"],
        "metrics_reconnects": metrics["reconnects"],
        "metrics_disconnects": metrics["disconnects"],
        "control_epoch_final": control["subscription_epoch_final"],
        "control_reconnects": control["metrics"]["reconnects"],
    }


# ---------------------------------------------------------------------------
# PROPERTY 2 — the one CP7 failed
# ---------------------------------------------------------------------------

def property_2(reconnect: dict) -> dict:
    """Per market, independently, at every boundary. Asserted on SHAPE."""
    timeline = reconnect["publishability_timeline"]
    universe = set(reconnect["config"]["market_tickers"])
    epochs = sorted({e["subscription_epoch"] for e in timeline})
    boundary_epochs = [e for e in epochs if e > 1]
    check(boundary_epochs, "no boundary epoch in the timeline", epochs)

    per_epoch = {}
    for epoch in boundary_epochs:
        acq = acquisitions(timeline, epoch=epoch)

        # (a) THE FAILURE SIGNATURE. One entry carrying many acquisitions is
        #     the CP7 defect, whatever the count.
        worst = max((a["acquisitions_in_this_entry"] for a in acq), default=0)
        check(worst == 1,
              f"epoch {epoch}: one frame republished {worst} markets at once — "
              "that is the CP7 failure shape",
              Counter(a["acquisitions_in_this_entry"] for a in acq))

        # (b) Each acquisition caused by ITS OWN snapshot.
        for a in acq:
            check(a["cause_type"] == "orderbook_snapshot",
                  f"epoch {epoch}: a market became publishable on a "
                  f"{a['cause_type']} frame", a)
            check(a["caused_by"] == a["market"],
                  f"epoch {epoch}: {a['market']} was republished on "
                  f"{a['caused_by']}'s snapshot — a sibling's snapshot says "
                  "nothing about this ladder", a)
            check(a["based_generation"] == a["subscription_generation"] == epoch,
                  f"epoch {epoch}: a market published while based elsewhere", a)

        # (c) Every market re-acquired exactly once, and no market was missed —
        #     otherwise "no bad acquisitions" could mean "no acquisitions".
        counts = Counter(a["market"] for a in acq)
        check(set(counts) == universe,
              f"epoch {epoch}: not every market re-acquired",
              {"missing": sorted(universe - set(counts)),
               "unexpected": sorted(set(counts) - universe)})
        check(set(counts.values()) == {1},
              f"epoch {epoch}: a market acquired more than once",
              {k: v for k, v in counts.items() if v != 1})

        # (d) THE OTHER HALF OF THE INVARIANT, and the half CP7 actually broke:
        #     while one market held its new base, the rest must be UNPUBLISHED
        #     under the typed boundary reason — not silently carried over.
        first = min(a["frame_ordinal"] for a in acq)
        rebase = [e for e in timeline if e["frame_ordinal"] == first][0]
        left_behind = [c for c in rebase["changes"]
                       if c["to_state"]["state"] == AWAITING]
        check(len(left_behind) == len(universe) - 1,
              f"epoch {epoch}: the first new-generation snapshot did not leave "
              "every other market awaiting its own",
              {"awaiting": len(left_behind), "expected": len(universe) - 1})
        for c in left_behind:
            check(c["to_state"]["based_generation"] == epoch - 1,
                  f"epoch {epoch}: a left-behind book does not carry the "
                  "abandoned epoch", c)

        per_epoch[epoch] = {
            "acquisitions": len(acq),
            "max_acquisitions_in_one_entry": worst,
            "entries_carrying_an_acquisition":
                len({a["frame_ordinal"] for a in acq}),
            "first_new_generation_snapshot_frame": first,
            "markets_left_awaiting_their_own_snapshot": len(left_behind),
            "acquisition_span_frames":
                max(a["frame_ordinal"] for a in acq) - first,
        }

    return {"verdict": "PROVEN", "universe": len(universe),
            "boundary_epochs": boundary_epochs, "per_epoch": per_epoch}


# ---------------------------------------------------------------------------
# PROPERTY 3 — anti-vacuity on live data
# ---------------------------------------------------------------------------

def property_3(drop: dict, control: dict) -> dict:
    """A real within-generation gap must still fault, and be typed as a FAULT."""
    withheld = [j for j in drop["perturbation_journal"]
                if j["event"] == "dropped_frame"]
    check(len(withheld) == 1, "expected exactly one withheld frame", withheld)

    check(drop["subscription_epoch_final"] == 1,
          "the drop session crossed a generation boundary; then it is not a "
          "WITHIN-generation test", drop["subscription_epoch_final"])
    check(drop["metrics"]["sequence_gaps"] >= 1,
          "the gap metric did not move", drop["metrics"]["sequence_gaps"])
    check(drop["session_result"]["sequence_faults"] >= 1,
          "the session counted no fault", drop["session_result"])
    check(drop["session_result"]["recoveries_requested"] == 1,
          "expected exactly one recovery for one gap",
          drop["session_result"]["recoveries_requested"])

    # THE POINT OF THIS CHECK. The fix added a benign typed state; a real fault
    # must NOT land in it.
    timeline = drop["publishability_timeline"]
    halts = [e for e in timeline
             if any(c["to_state"]["state"] == HALTED for c in e["changes"])]
    check(halts, "no book was ever halted by the withheld frame", len(timeline))
    halt = halts[0]
    states = Counter(c["to_state"]["state"] for c in halt["changes"])
    check(set(states) == {HALTED},
          "the gap put some book into a state other than book_halted", states)
    check(states[HALTED] == len(drop["config"]["market_tickers"]),
          "the gap did not unpublish every book on the subscription", states)
    check(halt["cause"]["seq"] == withheld[0]["seq"] + 1,
          "the halt was not caused by the frame after the hole",
          {"halt_seq": halt["cause"]["seq"], "withheld": withheld[0]["seq"]})

    # No market may be filed under the BENIGN boundary reason in a session
    # with no boundary at all.
    absorbed = [c for e in timeline for c in e["changes"]
                if c["to_state"]["state"] == AWAITING]
    check(not absorbed,
          "a within-generation fault was reported as the benign boundary "
          "state — the new typed state absorbed a real fault", len(absorbed))

    # THE PAIRED CONTROL: the same universe, unperturbed, halts nothing.
    control_halts = [c for e in control["publishability_timeline"]
                     for c in e["changes"]
                     if c["to_state"]["state"] == HALTED]
    check(not control_halts,
          "the unperturbed control halted a book; then the halt is not a "
          "property of the drop", len(control_halts))
    check(control["session_result"]["sequence_faults"] == 0,
          "the unperturbed control faulted",
          control["session_result"]["sequence_faults"])

    # Recovery is still per-market and still inside one generation.
    reacquired = acquisitions(timeline, epoch=1)
    after = [a for a in reacquired if a["frame_ordinal"] > halt["frame_ordinal"]]
    for a in after:
        check(a["caused_by"] == a["market"] and a["cause_type"] ==
              "orderbook_snapshot",
              "a market re-acquired on something other than its own snapshot", a)
        check(a["acquisitions_in_this_entry"] == 1,
              "the recovery republished several markets at once", a)

    return {
        "verdict": "PROVEN",
        "withheld": withheld[0],
        "halt_frame_ordinal": halt["frame_ordinal"],
        "halt_cause_seq": halt["cause"]["seq"],
        "books_halted": states[HALTED],
        "typed_state": HALTED,
        "reported_as_benign_boundary_state": len(absorbed),
        "metrics_sequence_gaps": drop["metrics"]["sequence_gaps"],
        "session_sequence_faults": drop["session_result"]["sequence_faults"],
        "recoveries_requested": drop["session_result"]["recoveries_requested"],
        "per_market_re_acquisitions_after_the_halt": len(after),
        "control_halts": len(control_halts),
        "control_faults": control["session_result"]["sequence_faults"],
    }


# ---------------------------------------------------------------------------
# PROPERTY 4 — reported, never asserted
# ---------------------------------------------------------------------------

def property_4(reconnect: dict) -> dict:
    """Was the delta-refusal guard EXERCISED, or merely not contradicted?"""
    refusals = reconnect.get("generation_delta_refusals")
    if refusals is None:
        return {"verdict": "NOT MEASURED",
                "note": "this artifact predates the refusal observer; absence "
                        "here is not a zero (doctrine 10)"}

    books = [b for sid in reconnect["live_terminal_state"].values()
             for b in sid["books"].values()]
    counted = sum(b["stats"].get("rejected_pre_generation_snapshot", 0)
                  for b in books)

    # The exposure window luck had to cover: how many frames the venue took to
    # re-snapshot every market after each boundary. Reported so the reader can
    # size what was NOT tested, rather than being told it did not matter.
    windows = {}
    timeline = reconnect["publishability_timeline"]
    for epoch in sorted({e["subscription_epoch"] for e in timeline}):
        if epoch <= 1:
            continue
        acq = acquisitions(timeline, epoch=epoch)
        if acq:
            ordinals = [a["frame_ordinal"] for a in acq]
            windows[epoch] = {"first": min(ordinals), "last": max(ordinals),
                              "span_frames": max(ordinals) - min(ordinals)}

    if refusals or counted:
        return {"verdict": "EXERCISED", "observed_refusals": len(refusals),
                "rejected_pre_generation_snapshot_total": counted,
                "refusals": refusals,
                "re_snapshot_windows": windows}
    return {
        "verdict": "NOT EXERCISED — the guard was not contradicted, which is "
                   "NOT the same as proven",
        "observed_refusals": 0,
        "rejected_pre_generation_snapshot_total": 0,
        "re_snapshot_windows": windows,
        "note": "No new-generation delta arrived for a market before that "
                "market's own new-generation snapshot, so the refusal path "
                "was never entered. This is a statement about the VENUE'S "
                "FRAME ORDERING in this session, not about the guard. The "
                "observer that would have recorded a refusal is proved to "
                "work by a forced positive control in "
                "tests/test_kalshi_cp6_cp9_functional_001.py::"
                "TestTheGenerationDeltaRefusalIsObservable, so an empty list "
                "here means 'did not fire', not 'nothing was watching'.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reconnect", default="s2-reconnect-session.json")
    parser.add_argument("--control", default="s1-observe-session.json")
    parser.add_argument("--drop", default="s3-drop-session.json")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    reconnect, control, drop = (load(args.reconnect), load(args.control),
                                load(args.drop))
    report = {
        "milestone": "KALSHI-CP7-LIVE-RERUN",
        "scope_note": SCOPE_NOTE,
        "sessions": {
            "reconnect": reconnect["run_label"],
            "control": control["run_label"],
            "drop": drop["run_label"],
        },
        "property_1_generation_advances": property_1(reconnect, control),
        "property_2_per_market_independence": property_2(reconnect),
        "property_3_within_generation_gap_still_faults":
            property_3(drop, control),
        "property_4_delta_refusal_path": property_4(reconnect),
    }
    text = json.dumps(report, indent=2, sort_keys=True)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n")


if __name__ == "__main__":
    main()
