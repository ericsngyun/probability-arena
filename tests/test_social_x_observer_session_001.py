"""SOLANA-SOCIAL-OBSERVER-QUALIFICATION-001 — universe freeze + event lifecycle.

Two contracts:

1. the frozen 18-source universe obeys the preregistration, and cannot be
   activated while a handle would silently match nothing;
2. the transport REPORTS and the driver INTERPRETS -- the transport is
   structurally incapable of mutating stream state.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from app.social import observer_session as OS
from app.social import x_transport as XT
from app.social import x_universe as XU
from app.social.observer_session import BudgetPolicy, ObserverSessionDriver
from app.social.x_events import (
    AuthenticationAccepted, ConnectionEnded, FrameObserved, HttpErrorObserved,
    PlatformErrorObserved, RateLimited, RetryBudgetExhausted, RulesReconciled,
    StreamOpened,
)
from app.social.x_stream_state import StreamState as S, TransitionReason as R
from app.social.x_universe import (
    CLASS_QUOTAS, FORBIDDEN_CRITERIA, FrozenXUniverse, HandleUnresolvedError,
    Population, UniverseContractError, XSourceRule, load_frozen_universe,
)

RAW = json.loads(XU.FROZEN_UNIVERSE_PATH.read_text())
TRANSPORT_SRC = Path(XT.__file__).read_text(encoding="utf-8")
DRIVER_SRC = Path(OS.__file__).read_text(encoding="utf-8")


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def opened(driver):
    driver.consume(AuthenticationAccepted())
    driver.consume(RulesReconciled(added=18))
    driver.consume(StreamOpened())


# --------------------------------------------------------------------------
# 1. THE FROZEN UNIVERSE
# --------------------------------------------------------------------------


class TestFrozenUniverse:
    def test_it_loads_and_is_eighteen_sources(self):
        assert len(load_frozen_universe()) == 18

    def test_class_quotas_match_the_preregistration_exactly(self):
        u = load_frozen_universe()
        counts = {}
        for r in u.rules:
            counts[r.source_class] = counts.get(r.source_class, 0) + 1
        assert counts == CLASS_QUOTAS
        assert sum(CLASS_QUOTAS.values()) == 18

    def test_no_rationale_cites_a_forbidden_selection_criterion(self):
        for r in load_frozen_universe().rules:
            low = r.rationale.lower()
            for bad in FORBIDDEN_CRITERIA:
                assert bad not in low, f"{r.rule_id} cites {bad!r}"

    def test_the_universe_is_natural_live_and_control_cannot_enter_it(self):
        u = load_frozen_universe()
        assert u.population is Population.NATURAL_LIVE
        assert "CONTROL" not in XU.FROZEN_UNIVERSE_PATH.read_text()

    def test_refusal_generating_classes_are_shape_rules_not_named_accounts(self):
        """Naming an account as ticker-only or as an impersonator asserts an
        unverifiable fact about a real entity. The authority resolver makes
        that call at run time; the rule only surfaces the candidate."""
        u = load_frozen_universe()
        refusal = [r for r in u.rules if r.is_refusal_generating]
        assert len(refusal) == 4
        assert all(r.is_shape_rule and r.handle is None for r in refusal)

    def test_both_refusal_classes_are_represented(self):
        """A qualification in which nothing is refused has not tested the
        refusal paths."""
        classes = {r.source_class for r in load_frozen_universe().rules
                   if r.is_refusal_generating}
        assert classes == XU.REFUSAL_GENERATING_CLASSES

    def test_rule_ids_are_unique(self):
        ids = [r.rule_id for r in load_frozen_universe().rules]
        assert len(set(ids)) == len(ids)

    def test_an_unresolved_handle_blocks_activation(self):
        """`from:` against a wrong handle matches nothing and is
        indistinguishable from a quiet source."""
        u = load_frozen_universe()
        assert len(u.unresolved_handles) == 14
        with pytest.raises(HandleUnresolvedError, match="unresolved"):
            u.assert_activatable()

    def test_resolution_comes_from_outside_and_then_activation_is_allowed(self):
        u = load_frozen_universe()
        resolved = {h: f"id-{h}" for h in u.unresolved_handles}
        u2 = u.with_resolved_handles(resolved)
        assert u2.unresolved_handles == ()
        u2.assert_activatable()

    def test_the_module_performs_no_network_resolution_itself(self):
        tree = ast.parse(Path(XU.__file__).read_text(encoding="utf-8"))
        imports = {a.name.split(".")[0] for n in ast.walk(tree)
                   if isinstance(n, ast.Import) for a in n.names}
        imports |= {(n.module or "").split(".")[0] for n in ast.walk(tree)
                    if isinstance(n, ast.ImportFrom)}
        assert imports <= {"__future__", "json", "dataclasses", "enum",
                           "pathlib", "typing"}


# --------------------------------------------------------------------------
# 2. THE TRANSPORT REPORTS; IT DOES NOT INTERPRET
# --------------------------------------------------------------------------


class TestSeparationOfConcerns:
    def test_the_transport_cannot_reach_the_state_machine(self):
        assert "x_stream_state" not in TRANSPORT_SRC

    def test_the_transport_never_calls_transition(self):
        tree = ast.parse(TRANSPORT_SRC)
        attrs = {n.attr for n in ast.walk(tree)
                 if isinstance(n, ast.Attribute)}
        for banned in ("transition", "note_frame", "report", "keepalive_stalled"):
            assert banned not in attrs, f"transport calls {banned}"

    def test_the_transport_holds_no_budget_or_cap(self):
        tree = ast.parse(TRANSPORT_SRC)
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(tree)
                  if isinstance(n, ast.Attribute)}
        for banned in ("max_posts", "budget", "min_wall_s", "stall_s"):
            assert not any(banned in n for n in names), banned

    def test_the_driver_opens_nothing_and_holds_no_credential(self):
        tree = ast.parse(DRIVER_SRC)
        imports = {a.name.split(".")[0] for n in ast.walk(tree)
                   if isinstance(n, ast.Import) for a in n.names}
        assert "httpx" not in imports
        assert "BearerToken" not in DRIVER_SRC

    def test_the_whole_lifecycle_replays_from_a_list(self):
        """The point of events: no socket is needed to test the accounting."""
        clock = Clock()
        d = ObserverSessionDriver(clock=clock)
        d.drive([AuthenticationAccepted(), RulesReconciled(added=18),
                 StreamOpened(), FrameObserved(is_post=True)])
        assert d.state is S.RECEIVING


# --------------------------------------------------------------------------
# 3. THE MAPPING
# --------------------------------------------------------------------------


class TestEventMapping:
    def test_a_keepalive_lifts_a_connection_into_receiving(self):
        """A frame of any kind proves liveness. That is why keepalives are
        frames rather than noise."""
        d = ObserverSessionDriver(clock=Clock())
        opened(d)
        assert d.state is S.STREAM_CONNECTED
        d.consume(FrameObserved(is_post=False))
        assert d.state is S.RECEIVING
        assert d.posts == 0

    def test_a_rate_limit_leaves_receiving(self):
        d = ObserverSessionDriver(clock=Clock())
        opened(d)
        d.consume(FrameObserved(is_post=True))
        d.consume(RateLimited())
        assert d.state is S.DEGRADED

    def test_a_platform_error_frame_degrades_without_counting_a_post(self):
        d = ObserverSessionDriver(clock=Clock())
        opened(d)
        d.consume(FrameObserved(is_post=True))
        d.consume(PlatformErrorObserved())
        assert d.state is S.DEGRADED
        assert d.posts == 1

    def test_recovery_from_degraded_returns_to_receiving(self):
        d = ObserverSessionDriver(clock=Clock())
        opened(d)
        d.consume(FrameObserved(is_post=True))
        d.consume(RateLimited())
        d.consume(FrameObserved(is_post=True))
        assert d.state is S.RECEIVING
        assert d.outcome().coverage.transitions[-1].reason is R.RECOVERED

    def test_a_clean_end_and_a_drop_both_stop_observation(self):
        for clean in (True, False):
            d = ObserverSessionDriver(clock=Clock())
            opened(d)
            d.consume(FrameObserved(is_post=True))
            d.consume(ConnectionEnded(clean=clean))
            assert d.state is S.RECONNECTING

    def test_an_http_error_is_reconnecting_not_silence(self):
        d = ObserverSessionDriver(clock=Clock())
        d.consume(AuthenticationAccepted())
        d.consume(RulesReconciled())
        d.consume(HttpErrorObserved(status=401))
        assert d.state is S.RECONNECTING
        assert d.outcome().coverage.observed_s == 0.0

    def test_foreign_rules_are_recorded_and_never_acted_on(self):
        d = ObserverSessionDriver(clock=Clock())
        d.consume(AuthenticationAccepted())
        d.consume(RulesReconciled(added=18, foreign=2))
        assert d.foreign_rules_seen == 2
        assert "delete" not in DRIVER_SRC

    def test_the_post_budget_stops_the_session(self):
        d = ObserverSessionDriver(budget=BudgetPolicy(max_posts=3),
                                  clock=Clock())
        opened(d)
        for _ in range(3):
            d.consume(FrameObserved(is_post=True))
        assert d.state is S.BUDGET_EXHAUSTED
        assert "budget stop" in d.outcome().coverage.why_zero() or d.posts == 3

    def test_the_budget_lives_in_the_driver_not_the_transport(self):
        assert "max_posts" in DRIVER_SRC
        assert "max_posts" not in TRANSPORT_SRC

    def test_a_wedged_stream_is_detected_by_tick_not_by_an_event(self):
        """A wedged connection emits nothing, including no event saying so."""
        clock = Clock()
        d = ObserverSessionDriver(clock=clock)
        opened(d)
        d.consume(FrameObserved(is_post=False))
        clock.advance(BudgetPolicy().keepalive_stall_s + 1)
        d.tick()
        assert d.state is S.DEGRADED
        assert d.outcome().coverage.transitions[-1].reason is R.KEEPALIVE_STALLED

    def test_the_wall_cap_is_not_masked_by_a_stalled_keepalive(self):
        """Past the stall window both conditions hold. The cap is absolute,
        so it must fire regardless -- this was an `elif` and did not."""
        clock = Clock()
        d = ObserverSessionDriver(budget=BudgetPolicy(max_wall_s=100.0),
                                  clock=clock)
        opened(d)
        d.consume(FrameObserved(is_post=False))
        clock.advance(BudgetPolicy().keepalive_stall_s + 1)
        assert d._machine.keepalive_stalled()      # the masking condition
        clock.advance(100.0)
        d.tick()
        assert d.state is S.STOPPED
        assert d.outcome().coverage.transitions[-1].reason is R.TIME_CAP_REACHED

    def test_the_wall_cap_stops_the_session(self):
        clock = Clock()
        d = ObserverSessionDriver(budget=BudgetPolicy(max_wall_s=100.0),
                                  clock=clock)
        opened(d)
        d.consume(FrameObserved(is_post=True))
        clock.advance(101.0)
        d.tick()
        assert d.state is S.STOPPED

    def test_an_event_after_stop_does_not_crash_the_run(self):
        """The machine still refuses the illegal history; tolerating the
        refusal is the driver's job, not the machine's."""
        d = ObserverSessionDriver(clock=Clock())
        opened(d)
        d.consume(FrameObserved(is_post=True))
        d.consume(RetryBudgetExhausted(attempts=9))
        assert d.state is S.STOPPED
        d.consume(RateLimited())
        assert d.state is S.STOPPED
        assert all(t.to_state is not S.DEGRADED
                   for t in d.outcome().coverage.transitions[-1:])

    def test_the_floors_are_reported_never_used_to_relabel(self):
        clock = Clock()
        d = ObserverSessionDriver(clock=clock)
        opened(d)
        d.consume(FrameObserved(is_post=True))
        clock.advance(10.0)
        out = d.outcome()
        assert out.reached_minimum_duration is False
        assert out.reached_minimum_artifacts is False
        assert out.qualification_complete is False
        assert out.coverage.posts_received == 1


# --------------------------------------------------------------------------
# 4. MUTATIONS
# --------------------------------------------------------------------------


class TestGuardsBite:
    def test_a_forbidden_criterion_in_a_rationale_is_caught(self):
        bad = dict(RAW)
        bad["rules"] = [dict(r) for r in RAW["rules"]]
        bad["rules"][0]["rationale"] = (
            "Included because of its very high follower count and strong "
            "historical track record of profitable calls.")
        with pytest.raises(UniverseContractError, match="forbidden"):
            XU._validate(bad, tuple(
                XSourceRule(rule_id=r["rule_id"], kind=r["kind"],
                            source_class=r["class"], selector=r["selector"],
                            rationale=r["rationale"],
                            active_from=r["active_from"], handle=r.get("handle"),
                            handle_resolved=bool(r["handle_resolved"]),
                            tags=tuple(r.get("tags") or ()))
                for r in bad["rules"]))

    def test_a_class_quota_drift_is_caught(self):
        bad = dict(RAW)
        bad["rules"] = [dict(r) for r in RAW["rules"]][:-1]
        with pytest.raises(UniverseContractError, match="quotas drifted"):
            XU._validate(bad, tuple(
                XSourceRule(rule_id=r["rule_id"], kind=r["kind"],
                            source_class=r["class"], selector=r["selector"],
                            rationale=r["rationale"],
                            active_from=r["active_from"], handle=r.get("handle"),
                            handle_resolved=bool(r["handle_resolved"]),
                            tags=tuple(r.get("tags") or ()))
                for r in bad["rules"]))

    def test_a_control_population_is_caught(self):
        bad = dict(RAW)
        bad["population"] = "CONTROL"
        with pytest.raises(UniverseContractError, match="NATURAL_LIVE"):
            XU._validate(bad, load_frozen_universe().rules)

    def test_naming_an_account_as_an_impersonator_is_caught(self):
        bad = dict(RAW)
        bad["rules"] = [dict(r) for r in RAW["rules"]]
        for r in bad["rules"]:
            if r["class"] == "impersonation_candidate_surface":
                r["handle"] = "someRealPerson"
                break
        with pytest.raises(UniverseContractError, match="shape rule"):
            XU._validate(bad, tuple(
                XSourceRule(rule_id=r["rule_id"], kind=r["kind"],
                            source_class=r["class"], selector=r["selector"],
                            rationale=r["rationale"],
                            active_from=r["active_from"], handle=r.get("handle"),
                            handle_resolved=bool(r["handle_resolved"]),
                            tags=tuple(r.get("tags") or ()))
                for r in bad["rules"]))

    def test_activating_on_an_unresolved_handle_is_caught(self):
        u = load_frozen_universe()
        stamped = FrozenXUniverse(frozen_at_utc=u.frozen_at_utc,
                                  population=u.population, rules=u.rules)
        with pytest.raises(HandleUnresolvedError):
            stamped.assert_activatable()

    def test_a_transport_that_drove_the_machine_directly_would_be_caught(self):
        mutated = TRANSPORT_SRC.replace(
            "from app.social.x_events import (",
            "from app.social.x_stream_state import StreamStateMachine\n"
            "from app.social.x_events import (", 1)
        assert "x_stream_state" in mutated, "mutation did not apply"
        assert "x_stream_state" not in TRANSPORT_SRC
