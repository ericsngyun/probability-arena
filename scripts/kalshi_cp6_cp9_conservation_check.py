"""KALSHI-CP6-CP9-FUNCTIONAL — CP8: conservation and deterministic replay.

Offline and pure: it opens no socket, holds no credential, touches no database
and makes no venue call. It takes the live session artifact written by
`kalshi_cp6_cp9_functional_probe.py` and the durable archive that session wrote,
and answers six questions separately, because merging them would let one pass
cover for another:

1. **integrity** — does the tape verify against its own digest chain?
2. **raw-frame conservation** — is every frame the collector received accounted
   for on disk, by count and by type?
3. **normalized-frame conservation** — re-running the SAME normalizer over the
   archived `raw` reproduces the archived `normalized`, exactly. This is what
   makes the normalized half of the record a derivation rather than a second,
   independently-drifting record.
4. **generation conservation** — the epochs the collector held are the epochs
   the tape carries.
5. **per-sid sequence findings conserved** — the ordering census rebuilt from
   the tape equals the census the live wire tap recorded.
6. **state equality** — `State_live^terminal == State_replay^terminal`.

**What `ticker` prevents.** The venue sends `ticker` with no `seq` at all
(2,071 of 2,071 on the P0 capture; re-measured here). For that channel there is
no ordering field, so questions 5's gap/duplicate/regression findings do not
exist to be conserved — and, critically, **the number of ticker frames the
venue sent can never be compared with the number we received.** What CAN be
established for ticker is conserved here and reported as such: the frames we
DID receive are all on disk, all re-normalize identically, and all carry the
generation they arrived under. Nothing more is claimed, and the check emits an
explicit `not_establishable` block rather than a silent omission.

**Two negative controls, because a comparison that cannot fail proves nothing**
(doctrine 7). Question 3 and question 6 are each re-run against a deliberately
corrupted copy of the records, and the check FAILS if the corrupted run still
reports equality.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from app.realtime.archive import EventArchive, replay  # noqa: E402
from app.realtime.book import GENERATION_UNKNOWN  # noqa: E402
from app.realtime.canonical import canonical_bytes, parse_canonical_datetime  # noqa: E402
from app.realtime.collector import normalize_frame  # noqa: E402


def _plain(value):
    """JSON-safe and LOSSLESS. `Decimal` renders with `str()`, never `float()`."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _tagged(value):
    """A comparison form that keeps the TYPE visible.

    A plain `str()` of a `Decimal` and the string that produced it render
    identically, so a comparison built on it would call a silent type change
    "equal". Every scalar therefore carries a one-letter tag, and a `float`
    tags as `F:` rather than being quietly accepted — the archive's own encoder
    refuses floats because they do not round-trip, and a checker that accepted
    one would be comparing two values neither of which is reversible.
    """
    if isinstance(value, bool):
        return f"B:{value}"
    if isinstance(value, Decimal):
        return f"D:{value}"
    if isinstance(value, float):
        return f"F:{value!r}"
    if isinstance(value, int):
        return f"I:{value}"
    if isinstance(value, str):
        return f"S:{value}"
    if value is None:
        return "N:"
    if isinstance(value, dict):
        return {str(k): _tagged(v) for k, v in sorted(value.items(),
                                                      key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_tagged(v) for v in value]
    return f"?:{value!r}"


def _canon(obj) -> str:
    """Deterministic, type-preserving, and it never raises on a real record."""
    return json.dumps(_tagged(obj), sort_keys=True, ensure_ascii=True)


def _canonical_encodable(obj) -> bool:
    """Would the archive's OWN encoder — the one the record digest is taken
    over — accept this value? Reported beside the equality result so a pass
    cannot rest on a comparison the durable format would have rejected."""
    try:
        canonical_bytes(obj)
        return True
    except Exception:                          # noqa: BLE001 - reported, not raised
        return False


# =================================================================================
# 2 + 5. the census, rebuilt from the tape
# =================================================================================


def tape_census(records) -> dict:
    """The per-sid ordering census, computed from the archive exactly as the
    live wire tap computes it from the socket.

    Deliberately a re-implementation of the SAME arithmetic over a different
    input, not a re-use of the live object: the point is that two independent
    passes over two different media reach the same numbers. Absence is counted
    as absence (`seq_absent`), never defaulted to a number — doctrine 10.
    """
    sids: dict = {}
    by_type: Counter = Counter()
    for r in records:
        etype = r.get("event_type") or "__no_type__"
        by_type[etype] += 1
        sid = r.get("sid")
        sid_key = sid if isinstance(sid, int) and not isinstance(sid, bool) else "__no_sid__"
        seq = r.get("seq")
        seq = seq if isinstance(seq, int) and not isinstance(seq, bool) else None
        entry = sids.get(sid_key)
        if entry is None:
            entry = sids[sid_key] = {
                "sid": sid_key, "frames": 0, "types": Counter(),
                "seq_absent": 0, "seq_first": None, "seq_last": None,
                "seq_contiguous": 0, "seq_gaps": 0, "seq_duplicates": 0,
                "seq_regressions": 0,
            }
        entry["frames"] += 1
        entry["types"][etype] += 1
        if seq is None:
            entry["seq_absent"] += 1
            continue
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
        if entry["seq_last"] is None or seq > entry["seq_last"]:
            entry["seq_last"] = seq
    return {
        "by_type": dict(by_type),
        "per_sid": {str(k): {**v, "types": dict(v["types"])}
                    for k, v in sorted(sids.items(), key=lambda kv: str(kv[0]))},
    }


# =================================================================================
# 3. normalization conservation
# =================================================================================


def normalization_conservation(records) -> dict:
    """Re-derive `normalized` from `raw` and require byte equality.

    `receive_time` is taken from the record's OWN `collector_receive_time`,
    because `_resolution_block` is a function of it — re-normalizing against
    "now" would compare two different quantities and call the difference a
    defect.
    """
    checked = mismatched = unparseable_time = 0
    not_encodable = 0
    examples = []
    by_channel: Counter = Counter()
    for r in records:
        stored = r.get("normalized")
        raw = r.get("raw")
        if not isinstance(stored, dict) or not isinstance(raw, dict):
            continue
        stamp = r.get("collector_receive_time")
        try:
            receive_time = parse_canonical_datetime(str(stamp))
        except Exception:                      # noqa: BLE001 - counted, not silent
            unparseable_time += 1
            continue
        recomputed = normalize_frame(message=raw, receive_time=receive_time)
        checked += 1
        by_channel[str(r.get("event_type"))] += 1
        if not _canonical_encodable(recomputed):
            not_encodable += 1
        if _canon(recomputed) != _canon(stored):
            mismatched += 1
            if len(examples) < 5:
                examples.append({
                    "event_type": r.get("event_type"), "sid": r.get("sid"),
                    "seq": r.get("seq"),
                    "stored": _plain(stored), "recomputed": _plain(recomputed)})
    return {"records_checked": checked, "mismatched": mismatched,
            "unparseable_receive_time": unparseable_time,
            "recomputed_not_canonically_encodable": not_encodable,
            "checked_by_event_type": dict(by_channel),
            "mismatch_examples": examples,
            "conserved": (mismatched == 0 and unparseable_time == 0
                          and not_encodable == 0 and checked > 0)}


# =================================================================================
# 4. generation conservation
# =================================================================================


def generation_conservation(records, session: dict) -> dict:
    sub = Counter()
    con = Counter()
    unknown_sub = unknown_con = 0
    for r in records:
        s = r.get("subscription_generation")
        c = r.get("connection_generation")
        if s is GENERATION_UNKNOWN or s is None:
            unknown_sub += 1
        else:
            sub[int(s)] += 1
        if c is GENERATION_UNKNOWN or c is None:
            unknown_con += 1
        else:
            con[int(c)] += 1
    live_epoch = session.get("subscription_epoch_final")
    live_conn = session.get("connection_generation_final")
    observed = sorted(sub)
    return {
        "tape_subscription_generations": {str(k): v for k, v in sorted(sub.items())},
        "tape_connection_generations": {str(k): v for k, v in sorted(con.items())},
        "records_with_unknown_subscription_generation": unknown_sub,
        "records_with_unknown_connection_generation": unknown_con,
        "live_subscription_epoch_final": live_epoch,
        "live_connection_generation_final": live_conn,
        "tape_max_subscription_generation": (max(observed) if observed else None),
        # The tape may legitimately hold FEWER epochs than the collector opened:
        # an epoch that produced no frame before the next one began stamped
        # nothing. It may never hold MORE, or one the collector never reached.
        "conserved": (
            bool(observed)
            and max(observed) <= (live_epoch or 0)
            and unknown_sub == 0
            and all(1 <= g <= (live_epoch or 0) for g in observed)),
    }


# =================================================================================
# 6. state equality
# =================================================================================


def live_flat_state(session: dict) -> dict:
    """Flatten the collector's terminal view to the quantities replay produces."""
    checksums, publishable, stats, sub_stats = {}, {}, {}, {}
    ladder_presence = {}
    for sid, entry in (session.get("live_terminal_state") or {}).items():
        sub_stats[str(sid)] = dict(entry.get("stats") or {})
        for ticker, book in (entry.get("books") or {}).items():
            checksums[ticker] = book.get("checksum")
            publishable[ticker] = bool(book.get("publishable"))
            stats[ticker] = dict(book.get("stats") or {})
            ladder_presence[ticker] = dict(book.get("ladder_presence") or {})
    return {"checksums": checksums, "publishable": publishable,
            "stats": stats, "subscription_stats": sub_stats,
            "ladder_presence": ladder_presence}


def compare_state(live: dict, out: dict) -> dict:
    """Every market, both directions. A market present on one side only is a
    difference, not a skip.

    Subscription statistics are compared only on the sids `archive.replay()`
    actually reconstructs — it returns early on every non-orderbook event type,
    so it never builds a router for the `trade` or `ticker` sid. That is a scope
    limitation of the replay FUNCTION and it is reported as one
    (`subscription_stats_live_only`), not absorbed into the market comparison
    and not silently dropped. What the TAPE can support for those sids is
    established separately by `subscription_findings_from_tape`.
    """
    tickers = sorted(set(live["checksums"]) | set(out["checksums"]))
    differences = []
    for t in tickers:
        lc, rc = live["checksums"].get(t, "__absent__"), out["checksums"].get(t, "__absent__")
        lp, rp = live["publishable"].get(t, "__absent__"), out["publishable"].get(t, "__absent__")
        ls, rs = live["stats"].get(t), out["stats"].get(t)
        if lc != rc or lp != rp or _canon(ls) != _canon(rs):
            differences.append({"market_ticker": t,
                                "live": {"checksum": lc, "publishable": lp, "stats": ls},
                                "replay": {"checksum": rc, "publishable": rp, "stats": rs}})
    shared = sorted(set(live["subscription_stats"]) & set(out["subscription_stats"]))
    sub_diff = []
    for sid in shared:
        a = live["subscription_stats"].get(sid)
        b = out["subscription_stats"].get(sid)
        if _canon(a) != _canon(b):
            sub_diff.append({"sid": sid, "live": a, "replay": b})
    return {
        "markets_live": len(live["checksums"]),
        "markets_replay": len(out["checksums"]),
        "markets_compared": len(tickers),
        "differences": differences,
        "subscription_sids_compared": shared,
        "subscription_stats_live_only": sorted(
            set(live["subscription_stats"]) - set(out["subscription_stats"])),
        "subscription_stats_replay_only": sorted(
            set(out["subscription_stats"]) - set(live["subscription_stats"])),
        "subscription_stat_differences": sub_diff,
        "equal": not differences and not sub_diff,
    }


def subscription_findings_from_tape(records) -> dict:
    """Re-derive EVERY sid's ordering findings from the tape.

    `archive.replay()` reconstructs books, so it visits orderbook records only.
    The live lane does more than that: it runs `SubscriptionRouter.dispatch`
    over every frame on every sid, which is why a `trade` gap is a live finding
    at all. This function closes that hole for the purpose of the conservation
    proof — same objects, same code, different medium — so the question "can
    the tape support the findings the live lane made?" is answered by
    measurement rather than by assumption.

    It is deliberately NOT a patch to `app/`. This milestone qualifies the
    collector; changing the replay function mid-qualification would mean
    qualifying something other than what shipped.

    Each subscription is seeded with the generation stamped on the FIRST record
    for its sid, exactly as `_Session._router_for` seeds it with the epoch
    current when the sid was first seen. Seeding at 1 instead would manufacture
    a `generation_advances` the collector never made.
    """
    from app.realtime.book import (
        SubscriptionError,
        SubscriptionRouter,
        SubscriptionState,
        subscription_generation_of,
    )

    routers: dict = {}
    faults = 0
    for r in records:
        sid = r.get("sid")
        if not isinstance(sid, int) or isinstance(sid, bool):
            continue
        router = routers.get(sid)
        if router is None:
            seed = subscription_generation_of(r)
            seed = seed if isinstance(seed, int) and seed >= 1 else 1
            router = routers[sid] = SubscriptionRouter(
                SubscriptionState(sid, generation=seed))
        try:
            router.dispatch(r)
        except SubscriptionError:
            faults += 1
        except Exception:                      # noqa: BLE001 - book-level refusal
            faults += 1
    # Doctrine 10, carried through the replay lane. `OrderBook.checksum()`
    # digests `{market_ticker, generation, last_seq, sid, yes, no}` and NOT
    # `ladder_presence`, so an equal checksum says nothing about whether a
    # zero-level side is "the venue said empty" or "the venue said nothing".
    # That distinction is the whole of doctrine 10, so it is compared here
    # explicitly rather than assumed to ride along with the digest.
    ladder_presence = {}
    for router in routers.values():
        for ticker, book in router.books.items():
            ladder_presence[ticker] = {str(k): str(v)
                                       for k, v in book.ladder_presence.items()}
    return {
        "faults": faults,
        "subscription_stats": {str(sid): dict(r.subscription.stats)
                               for sid, r in sorted(routers.items())},
        "carries_orderbook": {str(sid): r.subscription.carries_orderbook
                              for sid, r in sorted(routers.items())},
        "ladder_presence": ladder_presence,
    }


# =================================================================================
# negative controls
# =================================================================================


def corrupt_one_delta(records) -> tuple:
    """Move one applied delta's `seq` forward by one, in a COPY.

    That is the minimal edit that makes the tape disagree with itself: it opens
    a one-message hole in the subscription's sequence, which the replay lane is
    supposed to notice. If replay still reports the same terminal state, the
    equality test in question 6 is measuring nothing.
    """
    worst = copy.deepcopy(records)
    for i, r in enumerate(worst):
        if r.get("event_type") == "orderbook_delta" and isinstance(r.get("seq"), int):
            r["seq"] = r["seq"] + 1
            return worst, {"index": i, "sid": r.get("sid"),
                           "seq_after_corruption": r["seq"],
                           "market_ticker": r.get("market_ticker")}
    return worst, None


def corrupt_one_normalized(records) -> tuple:
    """Flip one stored `normalized.event_type`, in a COPY. Question 3 must
    notice; if it does not, byte equality is not being checked."""
    worst = copy.deepcopy(records)
    for i, r in enumerate(worst):
        n = r.get("normalized")
        if isinstance(n, dict) and "event_type" in n:
            n["event_type"] = str(n["event_type"]) + "__CORRUPTED"
            return worst, {"index": i, "event_type": r.get("event_type")}
    return worst, None


# =================================================================================


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-json", required=True)
    parser.add_argument("--archive", required=True)
    parser.add_argument("--environment", default="demo")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    session = json.loads(Path(args.session_json).read_text())
    store = EventArchive(Path(args.archive), environment=args.environment)
    integrity = store.verify()
    records = store.read_verified()

    result = session.get("session_result") or {}
    wire = session.get("wire") or {}
    live_wire_sids = {str(e["sid"]): e for e in (wire.get("per_sid_census") or [])}

    census = tape_census(records)

    # -- 2. raw-frame conservation ------------------------------------------------
    wire_by_type = dict(wire.get("by_type") or {})
    tape_by_type = census["by_type"]
    type_diff = {k: {"wire": wire_by_type.get(k, 0), "tape": tape_by_type.get(k, 0)}
                 for k in sorted(set(wire_by_type) | set(tape_by_type))
                 if wire_by_type.get(k, 0) != tape_by_type.get(k, 0)}
    raw_conservation = {
        "wire_frames_tapped": wire.get("frames_tapped"),
        "session_events_received": result.get("events_received"),
        "session_events_archived": result.get("events_archived"),
        "session_events_rejected": result.get("events_rejected"),
        "session_frames_malformed": result.get("frames_malformed"),
        "records_on_disk": len(records),
        "integrity_records": integrity.get("records"),
        # `received = archived + rejected + malformed` is the collector's own
        # stated conservation property; it is checked here rather than trusted.
        "received_equals_archived_plus_rejected_plus_malformed": (
            (result.get("events_received") or 0)
            == (result.get("events_archived") or 0)
                + (result.get("events_rejected") or 0)
                + (result.get("frames_malformed") or 0)),
        "wire_equals_received": wire.get("frames_tapped") == result.get("events_received"),
        "disk_equals_archived": len(records) == (result.get("events_archived") or 0),
        "per_event_type_differences": type_diff,
        "conserved": (
            wire.get("frames_tapped") == result.get("events_received")
            and len(records) == (result.get("events_archived") or 0)
            and not type_diff
            and (result.get("events_received") or 0)
            == (result.get("events_archived") or 0)
                + (result.get("events_rejected") or 0)
                + (result.get("frames_malformed") or 0)),
    }

    # -- 5. per-sid sequence findings ---------------------------------------------
    sid_findings = []
    for sid, tape_entry in census["per_sid"].items():
        live_entry = live_wire_sids.get(sid)
        fields = ("frames", "seq_absent", "seq_first", "seq_last",
                  "seq_contiguous", "seq_gaps", "seq_duplicates", "seq_regressions")
        if live_entry is None:
            sid_findings.append({"sid": sid, "live_census_present": False,
                                 "tape": {f: tape_entry.get(f) for f in fields},
                                 "agrees": False})
            continue
        mismatches = {f: {"wire": live_entry.get(f), "tape": tape_entry.get(f)}
                      for f in fields if live_entry.get(f) != tape_entry.get(f)}
        unsequenced = tape_entry["frames"] > 0 and tape_entry["seq_absent"] == tape_entry["frames"]
        sid_findings.append({
            "sid": sid,
            "live_census_present": True,
            "channels": tape_entry["types"],
            "frames": tape_entry["frames"],
            "seq_absent": tape_entry["seq_absent"],
            "unsequenced_channel": unsequenced,
            "mismatches": mismatches,
            "agrees": not mismatches,
            # Doctrine 10 applied to a WHOLE CHANNEL: for an unsequenced sid
            # there is no ordering finding to conserve, and saying "0 gaps"
            # would be a fabricated observation rather than a measured one.
            "ordering_findings_establishable": not unsequenced,
        })

    # -- 3. normalization ---------------------------------------------------------
    norm = normalization_conservation(records)
    bad_norm_records, norm_corruption = corrupt_one_normalized(records)
    norm_control = normalization_conservation(bad_norm_records)
    norm["negative_control"] = {
        "corruption": norm_corruption,
        "mismatched_under_corruption": norm_control["mismatched"],
        # The control must FAIL. A control that still says "conserved" means the
        # comparison above never compared anything.
        "control_detected_the_corruption": norm_control["mismatched"] > 0,
    }

    # -- 4. generations -----------------------------------------------------------
    gens = generation_conservation(records, session)

    # -- 6. state equality + determinism -----------------------------------------
    out_a = replay(records)
    out_b = replay(records)
    deterministic = _canon(_plain(out_a)) == _canon(_plain(out_b))

    live = live_flat_state(session)
    equality = compare_state(live, out_a)

    # The sids `archive.replay()` never builds a router for, re-derived from the
    # tape with the same objects the live lane used.
    tape_findings = subscription_findings_from_tape(records)
    live_sub = live["subscription_stats"]
    tape_sub = tape_findings["subscription_stats"]
    sub_recon_diff = []
    for sid in sorted(set(live_sub) | set(tape_sub)):
        if _canon(live_sub.get(sid)) != _canon(tape_sub.get(sid)):
            sub_recon_diff.append({"sid": sid, "live": live_sub.get(sid),
                                   "from_tape": tape_sub.get(sid)})
    live_ladder = live["ladder_presence"]
    tape_ladder = tape_findings["ladder_presence"]
    ladder_diff = [{"market_ticker": t, "live": live_ladder.get(t),
                    "from_tape": tape_ladder.get(t)}
                   for t in sorted(set(live_ladder) | set(tape_ladder))
                   if _canon(live_ladder.get(t)) != _canon(tape_ladder.get(t))]
    ladder_states_observed: Counter = Counter()
    for sides in live_ladder.values():
        for side, state in sides.items():
            ladder_states_observed[f"{side}={state}"] += 1

    findings_reconstructible = {
        "live_sids": sorted(live_sub),
        "tape_sids": sorted(tape_sub),
        "replay_function_sids": sorted(out_a["subscription_stats"]),
        "sids_the_shipped_replay_omits": sorted(set(tape_sub)
                                                - set(out_a["subscription_stats"])),
        "carries_orderbook_from_tape": tape_findings["carries_orderbook"],
        "differences": sub_recon_diff,
        "conserved": not sub_recon_diff and bool(tape_sub),
    }

    bad_records, corruption = corrupt_one_delta(records)
    out_bad = replay(bad_records)
    equality_control = compare_state(live, out_bad)

    verdict = {
        "milestone": "KALSHI-CP6-CP9-FUNCTIONAL",
        "check": "CP8 — conservation and deterministic replay",
        "scope_note": session.get("scope_note"),
        "session_json": str(args.session_json),
        "archive": str(args.archive),
        "mode": session.get("mode"),
        "run_label": session.get("run_label"),
        "integrity": integrity,
        "raw_frame_conservation": raw_conservation,
        "normalized_frame_conservation": norm,
        "generation_conservation": gens,
        "per_sid_sequence_findings": sid_findings,
        "replay": {
            "markets": out_a["markets"], "subscriptions": out_a["subscriptions"],
            "events_applied": out_a["events_applied"],
            "events_rejected": out_a["events_rejected"],
            "faults": out_a["faults"][:20], "fault_count": len(out_a["faults"]),
            "subscription_stats": out_a["subscription_stats"],
        },
        "replay_deterministic_across_two_runs": deterministic,
        "state_equality": equality,
        "subscription_findings_reconstructible_from_tape": findings_reconstructible,
        "ladder_presence_conservation": {
            "note": ("`OrderBook.checksum()` does NOT digest `ladder_presence`, "
                     "so an equal checksum is not evidence that the typed "
                     "absence survived the round trip. It is compared here on "
                     "its own."),
            "markets_compared": len(set(live_ladder) | set(tape_ladder)),
            "differences": ladder_diff,
            "live_states_observed": dict(ladder_states_observed),
            "conserved": not ladder_diff and bool(live_ladder),
        },
        "state_equality_negative_control": {
            "corruption": corruption,
            "still_equal_under_corruption": equality_control["equal"],
            "differences_detected": len(equality_control["differences"]),
            "replay_faults_under_corruption": len(out_bad["faults"]),
            # Must be True, i.e. the corrupted tape must NOT compare equal.
            "control_detected_the_corruption": not equality_control["equal"],
        },
        "ticker_limits": {
            "statement": (
                "The venue sends `ticker` with no `seq`. Three things follow, "
                "and only the first two are established here."),
            "established": [
                "every ticker frame the collector RECEIVED is on disk and is "
                "counted in raw-frame conservation",
                "every ticker frame re-normalizes byte-identically from its "
                "archived raw, and carries the generation it arrived under",
            ],
            "not_establishable": [
                "whether any ticker frame was LOST between the venue and the "
                "collector — there is no ordering field, so no gap, duplicate "
                "or regression finding exists to conserve",
                "therefore no completeness claim, and no feature derived from "
                "ticker may be described as lossless",
            ],
            "measured_seq_absent_by_sid": {
                s["sid"]: {"frames": s["frames"], "seq_absent": s["seq_absent"],
                           "channels": s.get("channels")}
                for s in sid_findings if s.get("unsequenced_channel")},
        },
    }

    checks = {
        "integrity_intact": bool(integrity.get("intact")),
        "raw_frame_conservation": raw_conservation["conserved"],
        "normalized_frame_conservation": norm["conserved"],
        "normalization_negative_control": norm["negative_control"][
            "control_detected_the_corruption"],
        "generation_conservation": gens["conserved"],
        "per_sid_findings_conserved": all(s["agrees"] for s in sid_findings),
        "subscription_findings_reconstructible_from_tape":
            findings_reconstructible["conserved"],
        "ladder_presence_conserved":
            verdict["ladder_presence_conservation"]["conserved"],
        "replay_deterministic": deterministic,
        "state_equality": equality["equal"],
        "state_equality_negative_control": verdict[
            "state_equality_negative_control"]["control_detected_the_corruption"],
    }
    verdict["checks"] = checks
    verdict["cp8_verdict"] = "QUALIFIED" if all(checks.values()) else "FAILED"

    Path(args.out).write_text(
        json.dumps(_plain(verdict), indent=2, sort_keys=True, default=str) + "\n")
    print(json.dumps({"cp8_verdict": verdict["cp8_verdict"], "checks": checks,
                      "out": str(args.out)}, indent=2))


if __name__ == "__main__":
    main()
