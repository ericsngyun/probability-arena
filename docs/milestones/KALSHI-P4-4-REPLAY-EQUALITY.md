# KALSHI-P4-4 — production replay equality: **QUALIFIED**

Computed 2026-08-19 over the **frozen** P4 production tape. Read-only: no
network, no credential, no venue, no capital, no new capture. The tape was not
modified, re-captured, or re-ordered.

---

## Why two arms, and why neither is sufficient alone

The production tape contains **zero `error` frames**, so replaying it cleanly
**cannot exercise the branch B3 repaired**. A clean production replay is
therefore evidence about production traffic and about nothing else. Conversely a
synthetic control says nothing about real venue behaviour. Both are required.

| arm | what it proves | where |
|---|---|---|
| **1 — frozen production tape** | replay reproduces live state on real venue traffic | `scripts/kalshi_p4_replay_equality.py` |
| **2 — wire-faithful B3 control** | the repaired branch is actually exercised | `tests/test_kalshi_p4_1_replay_requal_001.py` |

## Arm 1 — the frozen tape

84,170 records · 12 markets · 7 committed segments · one connection generation.

| check | result |
|---|---|
| `archive_integrity` | 84,170 records, **0 digest mismatches**, 0 truncated |
| `frame_conservation` | all **79,256** order-book frames applied |
| `no_replay_faults` | 0 rejected, 0 faults |
| `generation_conservation` | `subscription_generation=1`, `connection_generation=1`, every record stamped |
| `sequence_classification_per_sid` | sid 1 → **1..79,256 contiguous**; sid 3 → **1..2,516 contiguous** |
| `no_fabricated_b3_gap` | 0 markets report a gap; **0 markets halted** |
| `market_set_equality` | 12 markets on both sides |
| `terminal_state_equality` | **12 markets** equal on checksum, `publishable`, `publication_state`, `last_seq` and **9 stat counters** |

Evidence: `docs/evidence/KALSHI-P4-4-REPLAY-EQUALITY.json`.

**`recoveries` is excluded by contract**, not overlooked. P3 §8.2a: it counts an
outbound collector *action*, and the tape records inbound venue messages only.
Requiring equality there would require the tape to contain something it is
defined not to contain. A test asserts the harness does not compare it.

## Arm 2 — the B3 positive control

24 tests. The ones that carry this arm:

* `test_the_error_frame_advances_the_sequence_and_nothing_else` — the seq is
  consumed, the **ladder is not mutated**, and replay continues correctly.
* `test_replay_does_not_manufacture_a_gap` — the B3 failure itself.
* `test_a_drop_ADJACENT_to_an_error_frame_still_halts_the_book` — the drop
  detector is **not blinded** by the fix.
* `test_sequence_domains_stay_PER_SID` — a global counter would break every
  multi-sid tape.
* `test_CP8_state_equality_live_vs_replay` with
  `test_CP8_negative_control_still_fails` beside it.

## The harness can fail — demonstrated, then pinned

A qualification harness that cannot go red is a rubber stamp. Corrupting **one
checksum in one of the twelve markets** turns the verdict `NOT_QUALIFIED` with
an exact diff. That was demonstrated by hand against production and is now
guarded in CI by `tests/test_kalshi_p4_4_replay_equality_harness_001.py`, which
covers a wrong checksum, a wrong `publishable` flag, and — the vacuity case that
matters most — **zero markets compared, which must fail rather than pass**.

## One defect found in the harness itself

The first version counted `segment_id` off records returned by
`read_verified()`. That reader hands back the **envelope**, not the storage
record, so the field was `None` for every row and the census confidently
reported **1 segment for a tape holding 7**. Segment identity is a storage fact
and is now read from the committed directories. The raw tape settles it: six
rotations of **exactly 13,000 records** (`DEFAULT_MAX_SEGMENT_RECORDS`) plus the
one open at close — which independently confirms both the P4.2
`segments_committed` repair and that the rotation constant is doing its job.

## Scope

Qualifies the **reconstruction path** on one ten-minute overnight window. It
qualifies no rate, latency, capacity or microstructure claim, and L20 still
binds: one window is not a characterisation of the venue.
