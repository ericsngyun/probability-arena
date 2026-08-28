"""SOCIAL-X-LIVE-TRANSPORT-001 — the state machine that makes N=0 legible.

The whole point of this module is that `posts_received == 0` must mean
exactly one thing. These tests enumerate the five ways it could have been
ambiguous and assert each is separately identifiable.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.social import x_stream_state as XS
from app.social.x_stream_state import (
    KEEPALIVE_STALL_S,
    LEGAL_TRANSITIONS,
    OBSERVATION_BEARING,
    IllegalTransitionError,
    StreamState as S,
    StreamStateMachine,
    TransitionReason as R,
)

SOURCE = Path(XS.__file__).read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


class FakeClock:
    """Injected time. A machine that reads the wall clock cannot be tested
    for the thing it exists to measure."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def machine():
    clock = FakeClock()
    return StreamStateMachine(clock=clock), clock


def run_to_receiving(m, clock, *, connect_s=1.0):
    m.transition(S.AUTHENTICATED, R.AUTHENTICATED_OK)
    m.transition(S.RULES_RECONCILED, R.RULES_APPLIED)
    m.transition(S.STREAM_CONNECTED, R.CONNECT_OK)
    clock.advance(connect_s)
    m.transition(S.RECEIVING, R.FIRST_FRAME)


# --------------------------------------------------------------------------
# 1. THE FROZEN FACT
# --------------------------------------------------------------------------


class TestObservationBearing:
    def test_receiving_is_the_only_observation_bearing_state(self):
        assert OBSERVATION_BEARING == frozenset({S.RECEIVING})

    def test_degraded_time_is_neither_observation_nor_silence(self):
        """A rate-limited stream drops Posts we cannot enumerate, so its
        wall time can be claimed as neither."""
        m, clock = machine()
        run_to_receiving(m, clock)
        clock.advance(10.0)
        m.transition(S.DEGRADED, R.RATE_LIMITED)
        clock.advance(90.0)
        rep = m.report()
        assert rep.dwell_s[S.DEGRADED] == 90.0
        assert rep.observed_s == 10.0
        assert rep.system_failure_s >= 90.0

    def test_coverage_is_reported_never_judged(self):
        """No threshold lives in the transport. Sufficiency is a
        qualification decision."""
        names = {n.id for n in ast.walk(TREE) if isinstance(n, ast.Name)}
        names |= {n.name for n in ast.walk(TREE)
                  if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        for banned in ("min_coverage", "coverage_floor", "passes", "verdict",
                       "sufficient"):
            assert not any(banned in n.lower() for n in names)


# --------------------------------------------------------------------------
# 2. THE FIVE MEANINGS OF ZERO
# --------------------------------------------------------------------------


class TestZeroIsNotAmbiguous:
    def test_quiet_market_is_interpretable_silence(self):
        m, clock = machine()
        run_to_receiving(m, clock)
        clock.advance(3600.0)
        rep = m.report()
        assert rep.posts_received == 0
        assert rep.zero_posts_is_interpretable
        assert rep.observation_coverage > 0.99
        assert "silence over" in rep.why_zero()

    def test_never_connected_is_not_silence(self):
        m, clock = machine()
        m.transition(S.AUTHENTICATED, R.AUTHENTICATED_OK)
        clock.advance(3600.0)
        rep = m.report()
        assert rep.posts_received == 0
        assert not rep.zero_posts_is_interpretable
        assert rep.observation_coverage == 0.0
        assert "NOT SILENCE" in rep.why_zero()

    def test_authenticated_but_unavailable_is_not_a_quiet_market(self):
        """The distinction the whole module exists for."""
        m, clock = machine()
        m.transition(S.AUTHENTICATED, R.AUTHENTICATED_OK)
        m.transition(S.RULES_RECONCILED, R.RULES_APPLIED)
        m.transition(S.RECONNECTING, R.HTTP_ERROR)
        clock.advance(1800.0)
        rep = m.report()
        assert rep.observed_s == 0.0
        assert rep.system_failure_s == 1800.0
        assert "NOT SILENCE" in rep.why_zero()

    def test_rate_limited_is_not_natural_silence(self):
        m, clock = machine()
        run_to_receiving(m, clock)
        m.transition(S.DEGRADED, R.RATE_LIMITED)
        clock.advance(600.0)
        rep = m.report()
        assert rep.transitions[-1].reason is R.RATE_LIMITED
        assert rep.dwell_s.get(S.RECEIVING, 0.0) == 0.0
        assert rep.observation_coverage == 0.0

    def test_budget_exhausted_is_our_stop_not_a_provider_failure(self):
        m, clock = machine()
        run_to_receiving(m, clock)
        clock.advance(100.0)
        for _ in range(3000):
            m.note_frame(is_post=True)
        m.transition(S.BUDGET_EXHAUSTED, R.POST_BUDGET_REACHED)
        rep = m.report()
        assert rep.posts_received == 3000
        assert rep.terminal_state is S.BUDGET_EXHAUSTED
        assert "budget stop" in rep.why_zero() or rep.posts_received > 0
        assert rep.dwell_s.get(S.DEGRADED, 0.0) == 0.0

    def test_reconnecting_time_is_not_observation_coverage(self):
        m, clock = machine()
        run_to_receiving(m, clock)
        clock.advance(100.0)
        m.transition(S.RECONNECTING, R.CONNECTION_DROPPED)
        clock.advance(300.0)
        m.transition(S.STREAM_CONNECTED, R.CONNECT_OK)
        clock.advance(5.0)
        m.transition(S.RECEIVING, R.FIRST_FRAME)
        clock.advance(100.0)
        rep = m.report()
        assert rep.observed_s == 200.0
        assert rep.total_s == 506.0
        assert 0.39 < rep.observation_coverage < 0.40


# --------------------------------------------------------------------------
# 3. KEEPALIVES SEPARATE WEDGED FROM QUIET
# --------------------------------------------------------------------------


class TestKeepaliveStall:
    def test_a_keepalive_proves_liveness_without_being_a_post(self):
        m, clock = machine()
        run_to_receiving(m, clock)
        for _ in range(10):
            clock.advance(20.0)
            m.note_frame(is_post=False)
            assert not m.keepalive_stalled()
        assert m.posts_received == 0
        assert m.state is S.RECEIVING

    def test_total_silence_past_the_stall_window_is_detected(self):
        m, clock = machine()
        run_to_receiving(m, clock)
        m.note_frame(is_post=False)
        clock.advance(KEEPALIVE_STALL_S + 1.0)
        assert m.keepalive_stalled()

    def test_a_wedged_connection_leaves_receiving(self):
        """Without this, wedged-open and quiet are the same observation."""
        m, clock = machine()
        run_to_receiving(m, clock)
        clock.advance(KEEPALIVE_STALL_S + 1.0)
        assert m.keepalive_stalled()
        m.transition(S.DEGRADED, R.KEEPALIVE_STALLED)
        clock.advance(100.0)
        assert m.report().dwell_s[S.DEGRADED] == 100.0

    def test_the_stall_window_is_documented_as_unmeasured(self):
        """It is transcribed from protocol docs. The smoke corrects it."""
        idx = SOURCE.index("KEEPALIVE_STALL_S = ")
        note = SOURCE[max(0, idx - 900):idx]
        assert "TRANSCRIBED FROM PROTOCOL DOCUMENTATION" in note
        assert "not measured" in note


# --------------------------------------------------------------------------
# 4. THE TRANSITION TABLE
# --------------------------------------------------------------------------


class TestTransitionTable:
    def test_every_state_appears_in_the_table(self):
        assert set(LEGAL_TRANSITIONS) == set(S)

    def test_stopped_is_terminal(self):
        assert LEGAL_TRANSITIONS[S.STOPPED] == frozenset()

    def test_every_state_can_reach_stopped(self):
        for state, allowed in LEGAL_TRANSITIONS.items():
            if state is not S.STOPPED:
                assert S.STOPPED in allowed, state

    def test_receiving_cannot_be_entered_without_connecting(self):
        m, _ = machine()
        m.transition(S.AUTHENTICATED, R.AUTHENTICATED_OK)
        with pytest.raises(IllegalTransitionError):
            m.transition(S.RECEIVING, R.FIRST_FRAME)

    def test_an_illegal_transition_raises_rather_than_being_recorded(self):
        """A state history that cannot have happened is worse than a crash,
        because it gets averaged into a report."""
        m, _ = machine()
        with pytest.raises(IllegalTransitionError):
            m.transition(S.STREAM_CONNECTED, R.CONNECT_OK)
        assert m.state is S.UNCONFIGURED
        assert m.report().transitions == ()

    def test_budget_exhausted_cannot_resume(self):
        assert LEGAL_TRANSITIONS[S.BUDGET_EXHAUSTED] == frozenset({S.STOPPED})

    def test_dwell_time_is_conserved(self):
        """Artifact conservation, applied to seconds."""
        m, clock = machine()
        run_to_receiving(m, clock, connect_s=7.0)
        clock.advance(11.0)
        m.transition(S.RECONNECTING, R.CLEAN_EOF)
        clock.advance(13.0)
        rep = m.report()
        assert abs(sum(rep.dwell_s.values()) - rep.total_s) < 1e-9


# --------------------------------------------------------------------------
# 5. ALPHA BLINDNESS AND CAPABILITY
# --------------------------------------------------------------------------


class TestStaysInItsLane:
    def test_it_computes_no_return_markout_or_ranking(self):
        names = {n.id for n in ast.walk(TREE) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(TREE)
                  if isinstance(n, ast.Attribute)}
        for banned in ("markout", "pnl", "ranking", "rank_", "profit",
                       "price", "return_"):
            assert not any(banned in n.lower() for n in names), banned

    def test_it_holds_no_credential_and_opens_no_socket(self):
        imports = {a.name.split(".")[0] for n in ast.walk(TREE)
                   if isinstance(n, ast.Import) for a in n.names}
        imports |= {(n.module or "").split(".")[0] for n in ast.walk(TREE)
                    if isinstance(n, ast.ImportFrom)}
        assert imports <= {"__future__", "time", "dataclasses", "enum",
                           "typing"}


# --------------------------------------------------------------------------
# 6. MUTATIONS
# --------------------------------------------------------------------------


class TestGuardsBite:
    @staticmethod
    def _mutate(old: str, new: str):
        assert old in SOURCE, f"mutation target vanished: {old!r}"
        ns: dict = {}
        exec(compile(ast.parse(SOURCE.replace(old, new, 1)), "<mut>", "exec"),
             ns)
        return ns

    def test_widening_observation_bearing_is_caught(self):
        """The single change that would let a broken run pass as a quiet
        one. It must not survive."""
        ns = self._mutate(
            "OBSERVATION_BEARING = frozenset({StreamState.RECEIVING})",
            "OBSERVATION_BEARING = frozenset({StreamState.RECEIVING, "
            "StreamState.DEGRADED})")
        clock = FakeClock()
        m = ns["StreamStateMachine"](clock=clock)
        MS, MR = ns["StreamState"], ns["TransitionReason"]
        m.transition(MS.AUTHENTICATED, MR.AUTHENTICATED_OK)
        m.transition(MS.RULES_RECONCILED, MR.RULES_APPLIED)
        m.transition(MS.STREAM_CONNECTED, MR.CONNECT_OK)
        m.transition(MS.RECEIVING, MR.FIRST_FRAME)
        m.transition(MS.DEGRADED, MR.RATE_LIMITED)
        clock.advance(600.0)
        rep = m.report()
        # Under the mutation, 600s of rate-limited nothing reads as full
        # observation coverage. That is the defect, and it is visible.
        assert rep.observation_coverage == 1.0
        assert ns["OBSERVATION_BEARING"] != OBSERVATION_BEARING

    def test_an_unchecked_transition_table_is_caught(self):
        ns = self._mutate(
            "        if to not in LEGAL_TRANSITIONS[self._state]:",
            "        if False:")
        m = ns["StreamStateMachine"](clock=FakeClock())
        MS, MR = ns["StreamState"], ns["TransitionReason"]
        m.transition(MS.RECEIVING, MR.FIRST_FRAME)   # from UNCONFIGURED
        assert m.state is MS.RECEIVING, "the table guard was not the reason"

    def test_counting_keepalives_as_posts_is_caught(self):
        ns = self._mutate("        if is_post:\n            self._posts += 1",
                          "        self._posts += 1")
        m = ns["StreamStateMachine"](clock=FakeClock())
        m.note_frame(is_post=False)
        assert m.posts_received == 1, "keepalive/post confusion undetected"

    def test_dropping_dwell_accrual_is_caught(self):
        ns = self._mutate(
            """        self._dwell[self._state] = (
            self._dwell.get(self._state, 0.0) + (now - self._entered_at))""",
            "        pass")
        clock = FakeClock()
        m = ns["StreamStateMachine"](clock=clock)
        MS, MR = ns["StreamState"], ns["TransitionReason"]
        m.transition(MS.AUTHENTICATED, MR.AUTHENTICATED_OK)
        m.transition(MS.RULES_RECONCILED, MR.RULES_APPLIED)
        m.transition(MS.STREAM_CONNECTED, MR.CONNECT_OK)
        m.transition(MS.RECEIVING, MR.FIRST_FRAME)
        clock.advance(100.0)
        m.transition(MS.RECONNECTING, MR.CLEAN_EOF)
        clock.advance(100.0)
        rep = m.report()
        assert abs(sum(rep.dwell_s.values()) - rep.total_s) > 1e-9, (
            "conservation held under a mutation that should break it")

    def test_treating_absent_measurement_as_silence_is_caught(self):
        ns = self._mutate("        return self.observed_s > 0.0",
                          "        return True")
        clock = FakeClock()
        m = ns["StreamStateMachine"](clock=clock)
        MS, MR = ns["StreamState"], ns["TransitionReason"]
        m.transition(MS.AUTHENTICATED, MR.AUTHENTICATED_OK)
        clock.advance(3600.0)
        rep = m.report()
        assert rep.zero_posts_is_interpretable, "guard was not the reason"
        assert "NOT SILENCE" not in rep.why_zero()
