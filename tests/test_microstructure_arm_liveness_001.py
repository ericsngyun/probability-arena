"""MICROSTRUCTURE-ARM-LIVENESS-RECHECK-001 — market drift across the wait.

S04 died against 24 markets that had closed between selection and capture. The
guard written to prevent it was placed BEFORE the wait it describes, so it
could not catch its own scenario. S07 then died the same death: preflight
passed at 02:45Z with the candidates live, the script slept 100 minutes, and
by launch every KXMLBHR market had resolved with the game. The capture ran its
full three hours, produced 75 frames in the first 5.6 minutes, and exited 0.

A dead tape is worse than a refusal, because it does not look like a failure.
It looks like a quiet market.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / \
    "kalshi_microstructure_arm_session.py"
SOURCE = SCRIPT.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def load():
    spec = importlib.util.spec_from_file_location("_arm", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Fail:
    def __init__(self):
        self.messages = []

    def __call__(self, why):
        self.messages.append(why)
        return 1


class TestLivenessIsCheckedOnBothSidesOfTheWait:
    def test_a_wholly_dead_candidate_set_is_refused(self, monkeypatch):
        mod = load()
        monkeypatch.setattr(mod, "market_status", lambda t: "finalized")
        fail = Fail()
        assert mod.check_liveness(["A", "B"], when="launch", fail=fail) is False
        assert "closed or resolved" in fail.messages[0]
        assert "at launch" in fail.messages[0]

    def test_one_live_market_is_enough_to_proceed(self, monkeypatch):
        mod = load()
        monkeypatch.setattr(mod, "market_status",
                            lambda t: "active" if t == "A" else "finalized")
        fail = Fail()
        assert mod.check_liveness(["A", "B"], when="launch", fail=fail) is True
        assert fail.messages == []

    def test_an_unreadable_status_is_not_treated_as_dead(self, monkeypatch):
        """A status outage is our failure, not the market's. Refusing on it
        would let an endpoint blip cancel sessions whose markets are fine."""
        mod = load()
        monkeypatch.setattr(mod, "market_status", lambda t: None)
        fail = Fail()
        assert mod.check_liveness(["A", "B"], when="launch", fail=fail) is True

    def test_the_check_is_called_on_both_sides_of_the_sleep(self):
        """The structural claim: the S07 defect was PLACEMENT, so placement
        is what this asserts."""
        main = next(n for n in ast.walk(TREE)
                    if isinstance(n, ast.FunctionDef) and n.name == "main")
        order = []
        for node in ast.walk(main):
            if isinstance(node, ast.Call):
                fn = node.func
                if isinstance(fn, ast.Name) and fn.id == "check_liveness":
                    order.append((node.lineno, "liveness"))
                elif (isinstance(fn, ast.Attribute) and fn.attr == "sleep"):
                    order.append((node.lineno, "sleep"))
        kinds = [k for _, k in sorted(order)]
        assert kinds.count("liveness") == 2, kinds
        assert kinds.index("sleep") > 0, "no liveness check before the wait"
        assert kinds[-1] == "liveness", "no liveness check AFTER the wait"

    def test_the_status_constant_covers_what_the_venue_actually_reports(self):
        """`open` is a query filter; live markets report `active`. Getting
        this wrong once already refused 24 genuinely live markets."""
        mod = load()
        assert "active" in mod.LIVE_STATUSES


class TestGuardsBite:
    @staticmethod
    def _mutate(old: str, new: str):
        assert old in SOURCE, f"mutation target vanished: {old!r}"
        return ast.parse(SOURCE.replace(old, new, 1))

    @staticmethod
    def _exec(tree) -> dict:
        """Execute a mutated module. `__file__` is supplied because the
        script resolves paths at import time and would otherwise die for a
        reason unrelated to the mutation."""
        ns: dict = {"__file__": str(SCRIPT), "__name__": "_arm_mutant"}
        exec(compile(tree, str(SCRIPT), "exec"), ns)
        return ns

    def test_removing_the_post_wait_check_is_caught(self):
        """The exact S07 regression, reintroduced, and detected."""
        tree = self._mutate(
            '    if not check_liveness(markets, when="launch", fail=fail):\n'
            '        return 1\n',
            "")
        main = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "main")
        calls = [n for n in ast.walk(main) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name)
                 and n.func.id == "check_liveness"]
        assert len(calls) == 1, "the placement guard would not have noticed"

    def test_failing_open_on_a_dead_set_is_caught(self, monkeypatch):
        ns = self._exec(self._mutate(
            "    if not open_now and not unknown:", "    if False:"))
        ns["market_status"] = lambda t: "finalized"
        fail = Fail()
        assert ns["check_liveness"](["A"], when="launch", fail=fail) is True
        assert fail.messages == [], "the fail-closed guard was not the reason"

    def test_treating_unreadable_as_dead_is_caught(self, monkeypatch):
        ns = self._exec(self._mutate(
            "    if not open_now and not unknown:", "    if not open_now:"))
        ns["market_status"] = lambda t: None
        fail = Fail()
        assert ns["check_liveness"](["A"], when="launch", fail=fail) is False
