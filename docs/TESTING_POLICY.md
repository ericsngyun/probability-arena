# TESTING_POLICY

1. **Everything green before done.** `.venv/bin/python -m pytest -q` must pass in full. Never mark a milestone complete with failing or skipped-for-convenience tests.
2. **No live LLM or web calls in unit tests.** Every external provider (Kalshi, MLB Stats API, ESPN, DEX Screener, GoPlus, SolanaTracker, Anthropic) must be mocked/injected. The suite must run offline and without credentials; provider API keys must never appear in fixtures, logs, or CLI output (agent-context redaction is tested).
3. **Gated live tests skip by default.** `tests/test_live_kalshi.py` runs only with `RUN_LIVE_TESTS=true` and must stay out of CI paths.
4. **Live smoke ≠ unit tests.** Milestones additionally verify against real APIs manually (documented in the commit message), but that evidence never becomes a required test.
5. **Migrations require up/down tests.** Every Alembic revision gets: upgrade-creates assertions, downgrade-removes assertions, and the ORM-parity test must stay green. SQLite batch mode requires named constraints.
6. **Safety grep before completion** (clean = no implementation surface; acceptable hits: boundary docstrings, canon declarations, Kalshi WS auth, and the EVAL-001 scanner vocabulary constants in `frontier_eval.py`; the AST identifier-level audit in `frontier-eval-report --include-safety` is the stricter machine check):

   ```bash
   grep -rinE "expected_value|kelly|position_siz|paper_trad|place_order|submit_order|create_order|wallet|recommended_side|trade_recommend|execute_trade" app/ --include="*.py"
   ```
7. **Determinism is a feature.** Deterministic components (gates, template collectors/forecasters, scoring math, domain classification) get determinism tests (same input ⇒ identical output). Time-dependent logic must tolerate SQLite's naive-datetime round-trip.
8. **Fallbacks are tested, not assumed.** Every model-assisted or external path needs explicit tests for its failure/fallback behavior, and the fallback must be *honest* (template content stays labeled template).
9. **Central guarantees get adversarial tests.** Confidence caps, evidence-depth recomputation, and status gates are tested against deliberately misbehaving providers (e.g. an overconfident mock forecaster).
10. **Session/fixture hygiene.** In-memory SQLite with `StaticPool` when the TestClient is involved; each test file owns its fixtures; tests may import helpers across test modules but must not depend on execution order.

---

## Negative controls must exercise the guard they claim to test

**Rule.** A negative control must exercise a condition that **cannot already be
rejected by a different guard**. If deleting the mechanism under test leaves
the suite green, the test proves nothing about that mechanism, however
carefully it is written.

This is not hypothetical. It has now been caught three times in one milestone,
each time by mutation rather than by review:

| what looked tested | why it was not | how it was caught |
|---|---|---|
| absent values are not zero | the test asserted `m[k] is F.NOT_PROVIDED` — the absent value against *the very constant that defines absence*. Redefining `NOT_PROVIDED = 0.0` kept it green while every unobserved field became a real zero. | mutation survived |
| the 300 s embargo | every training row in the fixture also failed the *label-overlap* rule, so label-overlap did all the work. `cutoff = test_start` — the embargo deleted — passed 20/20. | mutation survived |
| the scheduler is activity-blind | a substring scan matched the module's own docstring, which *asserts* blindness. It could not tell an assertion from a violation. | test failed on a correct module |

**The shape of the failure is always the same:** the test and the code encode
the same assumption twice, so the test is satisfied by the assumption rather
than by the behaviour.

### How to write one that holds

1. **Name the mechanism, then delete it.** If the suite stays green, the test
   is decorative. Do this literally — mutate the source, run, restore, and
   verify the restore byte-for-byte.
2. **Choose a fixture only the mechanism under test can reject.** For an
   embargo, that means a row whose *label finishes well before* the test window
   — a row the overlap rule would happily keep.
3. **Assert the literal, not the constant.** `assert x is None`, never
   `assert x is SOME_SENTINEL`, when the point is what the sentinel *is*.
4. **Guard over structure, not text.** AST identifiers and non-docstring
   literals; source-text scans cannot distinguish a claim from a breach.
5. **State the property directly** alongside the example, e.g. "deleting the
   embargo changes the kept set", so the invariant cannot be satisfied by
   accident.

### Where this is binding

Any preregistered experiment, any guard protecting an experiment's integrity
(dataset-role locks, look-ahead controls, eligibility gates), and any
conservation or refusal path. For these, **a mutation campaign is part of the
test, not a review of it** — a suite that has not had its own mechanisms
deleted has not been checked.

---

## Never establish remote process existence with `pgrep -f`

**Rule.** Do not use `pgrep -f <pattern>` to decide whether a remote process
exists when the pattern appears in the SSH command line. It matches its own
invocation.

This has now fired three times in one milestone, and **every time in the
reassuring direction**:

| what it claimed | what was true |
|---|---|
| "CAPTURE STILL RUNNING — DO NOT MERGE" | no capture was running; the merge was safe |
| "MICROSTRUCTURE STILL RUNNING" | EVO was idle |
| "LAUNCHED" | S05 had not launched; it was still 42 minutes from its start |

A false *running* is worse than a false *stopped*: it makes us believe a
capture, a guard or a session exists when it does not, and it is exactly the
class of silent-wrongness this project keeps guarding against elsewhere.

### Use instead

* a PID the process itself recorded, confirmed with `kill -0`;
* systemd unit state, where the process is systemd-managed;
* a **script file on the remote host**, so the pattern never appears in the
  invocation;
* `/proc/<pid>/cmdline`, after resolving the PID from a non-self-referential
  source.

### For tranche sessions, prefer artifact-backed state

`scripts/kalshi_session_state.py` implements it. Process search is
**diagnostic only**; the state machine turns on artifacts the session itself
creates:

```text
ARMED     arm process alive, no session root yet
LAUNCHED  session root + genesis exist, capture PID recorded and alive
RUNNING   as LAUNCHED, and the archive is accumulating
CLOSED    terminal session artifact written
```

The existence of the archive is then part of the proof of launch, rather than
something inferred from a string match.

---

## A scripted source transformation must prove it transformed its target

**Rule.** Any scripted edit — `str.replace`, a codemod, a regex substitution,
`sed -i` — must assert that it matched, and then assert the intended
postcondition. Silence is not success.

```python
assert old in s, "TARGET NOT FOUND -- refusing to silently no-op"
s = s.replace(old, new, 1)
```

**Why this specific failure is nastier than it looks.** A `str.replace` that
matches nothing **returns the original string and raises nothing**. The script
exits 0, the file is rewritten identically, and every downstream signal says
the edit succeeded. When the Token-2022 fix was applied this way against a
pattern that had already been edited earlier, the patch silently did nothing —
and the subsequent live run showed the *old* behaviour, which looked exactly
like the fix having failed **on its merits**. The wrong debugging question
follows: "why didn't my logic work?" instead of "did my edit land?"

It is the same class as a vacuous test pass and a `pgrep -f` self-match:

> **operation reports success ≠ intended state changed**

### The three instances so far, all in one milestone

| operation | reported | actually |
|---|---|---|
| `str.replace` on an already-edited pattern | success, exit 0 | file unchanged |
| L2/L3 verdict on a session with zero ticks | `PASS` | nothing was exercised |
| `pgrep -f <pattern>` over SSH | process found | matched its own command line |

### Practice

* assert the target exists **before** replacing;
* prefer `assert match_count == expected`, not merely `> 0`, when the count is
  known;
* after the edit, assert the **postcondition** — grep for the new branch, import
  the module and check the attribute, run the test that should now change;
* never conclude a behaviour is wrong until you have confirmed the code that
  produces it is the code you think you edited.
