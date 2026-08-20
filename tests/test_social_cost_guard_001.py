"""SOCIAL-TAPE-001 — the cost guard must FAIL CLOSED.

Every test here forces a specific failure condition and asserts that spending
STOPS. A guard tested only in its healthy state proves only the healthy state
(AGENTS.md doctrine 7), so each refusal has a paired positive control showing
the guard permits the read when the condition is absent.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import stat

import pytest

from app.social.cost_guard import (
    BudgetExhaustedError,
    BudgetNotConfiguredError,
    CostBudget,
    CostGuardError,
    CounterUnreadableError,
    CounterUnwritableError,
    MonthlyReadCostGuard,
    PeriodRegressionError,
    current_period,
)


def at(year: int, month: int, day: int = 1):
    moment = dt.datetime(year, month, day, 12, 0, 0, tzinfo=dt.timezone.utc)
    return lambda: moment


class TestBudgetMustBeConfigured:
    def test_absent_budget_is_refused(self):
        with pytest.raises(BudgetNotConfiguredError):
            CostBudget.from_config(None)

    def test_zero_budget_is_refused(self):
        with pytest.raises(BudgetNotConfiguredError):
            CostBudget(max_reads_per_month=0)

    def test_negative_budget_is_refused(self):
        with pytest.raises(BudgetNotConfiguredError):
            CostBudget(max_reads_per_month=-1)

    def test_boolean_is_not_an_integer_budget(self):
        with pytest.raises(BudgetNotConfiguredError):
            CostBudget(max_reads_per_month=True)  # type: ignore[arg-type]

    def test_guard_refuses_a_bare_integer(self, tmp_path):
        with pytest.raises(BudgetNotConfiguredError):
            MonthlyReadCostGuard(tmp_path / "l.json", 1000)  # type: ignore[arg-type]

    def test_guard_refuses_a_none_budget(self, tmp_path):
        with pytest.raises(BudgetNotConfiguredError):
            MonthlyReadCostGuard(tmp_path / "l.json", None)  # type: ignore[arg-type]

    def test_positive_control_an_explicit_budget_is_accepted(self, tmp_path):
        guard = MonthlyReadCostGuard(
            tmp_path / "l.json", CostBudget.from_config(500)
        )
        assert guard.budget.max_reads_per_month == 500
        assert guard.assert_startable().consumed == 0

    def test_warn_line_must_lie_inside_the_cap(self):
        with pytest.raises(BudgetNotConfiguredError):
            CostBudget(max_reads_per_month=10, warn_at_reads=11)


class TestBudgetStopsSpending:
    def test_it_stops_exactly_at_the_cap(self, tmp_path):
        """THE test: prove it stops."""

        guard = MonthlyReadCostGuard(
            tmp_path / "l.json", CostBudget(max_reads_per_month=5)
        )
        for expected in range(1, 6):
            assert guard.reserve(1).consumed == expected

        with pytest.raises(BudgetExhaustedError):
            guard.reserve(1)
        # And it stays stopped — not a one-shot refusal.
        for _ in range(3):
            with pytest.raises(BudgetExhaustedError):
                guard.reserve(1)
        assert guard.remaining() == 0
        assert guard.read_state().consumed == 5

    def test_a_batch_that_would_overshoot_is_refused_entirely(self, tmp_path):
        guard = MonthlyReadCostGuard(
            tmp_path / "l.json", CostBudget(max_reads_per_month=10)
        )
        guard.reserve(8)
        with pytest.raises(BudgetExhaustedError):
            guard.reserve(3)
        # Partial spending is not permitted: the count is unchanged.
        assert guard.read_state().consumed == 8

    def test_an_exhausted_period_refuses_to_start(self, tmp_path):
        guard = MonthlyReadCostGuard(
            tmp_path / "l.json", CostBudget(max_reads_per_month=2)
        )
        guard.reserve(2)
        with pytest.raises(BudgetExhaustedError):
            guard.assert_startable()

    def test_the_count_survives_a_new_guard_instance(self, tmp_path):
        path = tmp_path / "l.json"
        MonthlyReadCostGuard(path, CostBudget(max_reads_per_month=4)).reserve(4)
        reborn = MonthlyReadCostGuard(path, CostBudget(max_reads_per_month=4))
        with pytest.raises(BudgetExhaustedError):
            reborn.reserve(1)

    def test_lowering_the_cap_mid_period_takes_effect_immediately(self, tmp_path):
        path = tmp_path / "l.json"
        MonthlyReadCostGuard(path, CostBudget(max_reads_per_month=100)).reserve(50)
        tightened = MonthlyReadCostGuard(path, CostBudget(max_reads_per_month=20))
        with pytest.raises(BudgetExhaustedError):
            tightened.reserve(1)

    def test_reserve_rejects_nonsense_counts(self, tmp_path):
        guard = MonthlyReadCostGuard(
            tmp_path / "l.json", CostBudget(max_reads_per_month=5)
        )
        for bad in (0, -1, True, 1.5):
            with pytest.raises(CostGuardError):
                guard.reserve(bad)  # type: ignore[arg-type]


class TestUnreadableCounterFailsClosed:
    def test_corrupt_json_stops_spending(self, tmp_path):
        path = tmp_path / "l.json"
        guard = MonthlyReadCostGuard(path, CostBudget(max_reads_per_month=10))
        guard.reserve(1)
        path.write_bytes(b"{not json")
        with pytest.raises(CounterUnreadableError):
            guard.reserve(1)
        with pytest.raises(CounterUnreadableError):
            guard.assert_startable()

    def test_truncated_file_stops_spending(self, tmp_path):
        path = tmp_path / "l.json"
        guard = MonthlyReadCostGuard(path, CostBudget(max_reads_per_month=10))
        guard.reserve(1)
        raw = path.read_bytes()
        path.write_bytes(raw[: len(raw) // 2])
        with pytest.raises(CounterUnreadableError):
            guard.reserve(1)

    def test_tampered_count_fails_the_integrity_check(self, tmp_path):
        """Editing consumed downward must not buy more reads."""

        path = tmp_path / "l.json"
        guard = MonthlyReadCostGuard(path, CostBudget(max_reads_per_month=10))
        guard.reserve(9)
        envelope = json.loads(path.read_text())
        envelope["body"]["consumed"] = 0
        path.write_text(json.dumps(envelope))
        with pytest.raises(CounterUnreadableError):
            guard.reserve(1)

    def test_missing_digest_stops_spending(self, tmp_path):
        path = tmp_path / "l.json"
        guard = MonthlyReadCostGuard(path, CostBudget(max_reads_per_month=10))
        guard.reserve(1)
        envelope = json.loads(path.read_text())
        del envelope["digest"]
        path.write_text(json.dumps(envelope))
        with pytest.raises(CounterUnreadableError):
            guard.reserve(1)

    def test_unknown_ledger_version_stops_spending(self, tmp_path):
        path = tmp_path / "l.json"
        guard = MonthlyReadCostGuard(path, CostBudget(max_reads_per_month=10))
        guard.reserve(1)
        envelope = json.loads(path.read_text())
        envelope["body"]["version"] = "something-else"
        path.write_text(json.dumps(envelope))
        with pytest.raises(CounterUnreadableError):
            guard.reserve(1)

    def test_negative_stored_count_stops_spending(self, tmp_path):
        from app.social.cost_guard import _digest

        path = tmp_path / "l.json"
        guard = MonthlyReadCostGuard(path, CostBudget(max_reads_per_month=10))
        guard.reserve(1)
        envelope = json.loads(path.read_text())
        envelope["body"]["consumed"] = -5
        envelope["digest"] = _digest(envelope["body"])
        path.write_text(json.dumps(envelope))
        with pytest.raises(CounterUnreadableError):
            guard.reserve(1)

    @pytest.mark.skipif(os.getuid() == 0, reason="root ignores file permissions")
    def test_unreadable_file_stops_spending(self, tmp_path):
        path = tmp_path / "l.json"
        guard = MonthlyReadCostGuard(path, CostBudget(max_reads_per_month=10))
        guard.reserve(1)
        path.chmod(0)
        try:
            with pytest.raises(CounterUnreadableError):
                guard.reserve(1)
        finally:
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    @pytest.mark.skipif(os.getuid() == 0, reason="root ignores file permissions")
    def test_unwritable_directory_stops_spending(self, tmp_path):
        directory = tmp_path / "ledger"
        directory.mkdir()
        path = directory / "l.json"
        guard = MonthlyReadCostGuard(path, CostBudget(max_reads_per_month=10))
        guard.reserve(1)
        directory.chmod(stat.S_IRUSR | stat.S_IXUSR)
        try:
            with pytest.raises(CounterUnwritableError):
                guard.reserve(1)
        finally:
            directory.chmod(stat.S_IRWXU)

    def test_positive_control_a_healthy_ledger_permits_the_read(self, tmp_path):
        guard = MonthlyReadCostGuard(
            tmp_path / "l.json", CostBudget(max_reads_per_month=10)
        )
        assert guard.reserve(1).consumed == 1


class TestPeriodBoundaries:
    def test_period_is_utc_calendar_month(self):
        assert current_period(
            dt.datetime(2026, 8, 20, 23, 59, tzinfo=dt.timezone.utc)
        ) == "2026-08"

    def test_naive_datetime_is_refused(self):
        with pytest.raises(CounterUnreadableError):
            current_period(dt.datetime(2026, 8, 20))

    def test_a_new_month_rolls_over_and_records_the_closed_period(self, tmp_path):
        path = tmp_path / "l.json"
        budget = CostBudget(max_reads_per_month=3)
        august = MonthlyReadCostGuard(path, budget, now=at(2026, 8))
        august.reserve(3)
        with pytest.raises(BudgetExhaustedError):
            august.reserve(1)

        september = MonthlyReadCostGuard(path, budget, now=at(2026, 9))
        state = september.reserve(1)
        assert state.period == "2026-09"
        assert state.consumed == 1
        assert state.previous_period == "2026-08"
        assert state.previous_consumed == 3

    def test_a_clock_moving_backwards_does_NOT_reset_the_budget(self, tmp_path):
        """The asymmetry that matters: forward rolls, backward refuses."""

        path = tmp_path / "l.json"
        budget = CostBudget(max_reads_per_month=3)
        MonthlyReadCostGuard(path, budget, now=at(2026, 9)).reserve(3)

        rewound = MonthlyReadCostGuard(path, budget, now=at(2026, 8))
        with pytest.raises(PeriodRegressionError):
            rewound.reserve(1)
        with pytest.raises(CounterUnreadableError):
            rewound.assert_startable()


class TestPreIncrementOrdering:
    def test_the_counter_is_written_before_the_read_is_permitted(self, tmp_path):
        """Reserve is durable at the moment it returns.

        Simulates a crash immediately after reserve() by discarding the guard
        and re-reading from disk: the spend must already be recorded, because
        over-counting is the only safe error direction.
        """

        path = tmp_path / "l.json"
        budget = CostBudget(max_reads_per_month=10)
        MonthlyReadCostGuard(path, budget).reserve(1)
        del budget
        reborn = MonthlyReadCostGuard(path, CostBudget(max_reads_per_month=10))
        assert reborn.read_state().consumed == 1

    def test_a_refused_reserve_does_not_advance_the_counter(self, tmp_path):
        path = tmp_path / "l.json"
        guard = MonthlyReadCostGuard(path, CostBudget(max_reads_per_month=2))
        guard.reserve(2)
        for _ in range(5):
            with pytest.raises(BudgetExhaustedError):
                guard.reserve(1)
        assert guard.read_state().consumed == 2
