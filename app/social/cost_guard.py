"""SOCIAL-TAPE-001 — the monthly read budget, enforced fail-closed.

X post reads are priced per read against a monthly cap. A collector that can
exceed that cap is a collector that can produce an unbounded bill from a bug,
a reconnect storm, or a backfill loop. So the budget is not advisory: it is a
persisted counter that is incremented BEFORE consumption and consulted before
every read, and every ambiguous state resolves to REFUSE.

The four refusal conditions
---------------------------
1. **No budget configured.** Not "unlimited", not "a sensible default" — the
   collector refuses to start. A default budget is a budget nobody chose.
2. **Budget exhausted.** Consumption stops at the cap, not near it.
3. **Counter unreadable.** Missing directory, permission error, truncated
   file, malformed JSON, failed integrity check — anything that means "we
   cannot state how much has been spent" is treated as "we may have spent it
   all". A guard that opens when it cannot see is not a guard.
4. **Counter unwritable.** If the increment cannot be durably recorded, the
   read does not happen. We would rather lose a post than lose the count.

Why pre-increment
-----------------
The counter is incremented and fsynced *before* the read is performed. A crash
between the increment and the read over-counts by at most the in-flight
reservation. The opposite ordering under-counts, and under-counting is the only
error direction that can produce a bill nobody authorized.

Cap changes mid-period
----------------------
The effective cap is ``min(stored_budget, configured_budget)``, so LOWERING the
cap takes effect immediately, while RAISING it does not take effect until the
next period. That asymmetry is the same one as rollover, for the same reason:
the direction that can only reduce spending is applied at once, and the
direction that can only increase it waits for a period boundary where someone
had to look at the number again.

Rollover
--------
The period is a UTC calendar month, ``YYYY-MM``. A new period is entered only
when the observed month is strictly *after* the stored one; a month that moves
*backwards* (clock skew, a restored backup, a mis-set host clock) is a refusal,
not a free reset. That asymmetry is deliberate: forward motion at worst grants
a budget that was going to be granted anyway, backward motion would grant one
that was already spent.

CONTAINS NO SIGNAL. This module counts money-shaped API calls; it has no view
on what is worth reading.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from app.realtime.book import utcnow

__all__ = [
    "CostGuardError",
    "BudgetNotConfiguredError",
    "BudgetExhaustedError",
    "CounterUnreadableError",
    "CounterUnwritableError",
    "PeriodRegressionError",
    "CostBudget",
    "CostLedgerState",
    "MonthlyReadCostGuard",
    "current_period",
]

_LEDGER_VERSION = "social-cost-guard.v1"


class CostGuardError(Exception):
    """Base class. Every subclass means: do not perform the read."""


class BudgetNotConfiguredError(CostGuardError):
    """No explicit monthly cap was configured. The collector must not start."""


class BudgetExhaustedError(CostGuardError):
    """The configured monthly cap has been reached."""


class CounterUnreadableError(CostGuardError):
    """The persisted counter could not be read or could not be trusted."""


class CounterUnwritableError(CostGuardError):
    """The reservation could not be durably recorded."""


class PeriodRegressionError(CounterUnreadableError):
    """The observed month precedes the stored month.

    A subclass of unreadable rather than its own top-level condition, because
    the operational meaning is identical: we cannot state how much has been
    spent in the period we think we are in.
    """


def current_period(now: datetime | None = None) -> str:
    """UTC calendar month, ``YYYY-MM``. Local time is never used."""

    moment = now or utcnow()
    if moment.tzinfo is None:
        raise CounterUnreadableError(
            "a naive datetime cannot identify a UTC billing period"
        )
    moment = moment.astimezone(timezone.utc)
    return f"{moment.year:04d}-{moment.month:02d}"


@dataclass(frozen=True)
class CostBudget:
    """An explicitly configured monthly cap.

    There is no default value and no ``unlimited`` variant. Constructing one
    requires someone to have typed a number.
    """

    max_reads_per_month: int
    #: Optional soft line for reporting only. It changes no decision; it exists
    #: so operators can see the wall coming without the guard behaving
    #: differently near it.
    warn_at_reads: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.max_reads_per_month, int) or isinstance(
            self.max_reads_per_month, bool
        ):
            raise BudgetNotConfiguredError(
                "max_reads_per_month must be an explicit integer"
            )
        if self.max_reads_per_month <= 0:
            raise BudgetNotConfiguredError(
                "a monthly cap of zero or less is not a configuration, it is "
                "an absent one; the collector refuses to start"
            )
        if self.warn_at_reads is not None and not (
            0 < self.warn_at_reads <= self.max_reads_per_month
        ):
            raise BudgetNotConfiguredError(
                "warn_at_reads must lie inside the cap"
            )

    @classmethod
    def from_config(cls, value: int | None, *, warn_at: int | None = None) -> "CostBudget":
        """Build from configuration, refusing absence loudly."""

        if value is None:
            raise BudgetNotConfiguredError(
                "no monthly read budget is configured; set an explicit cap "
                "before any collector may start"
            )
        return cls(max_reads_per_month=int(value), warn_at_reads=warn_at)


@dataclass(frozen=True)
class CostLedgerState:
    """What the persisted counter says right now."""

    period: str
    consumed: int
    budget: int
    #: Final consumed count of the most recent closed period, carried forward
    #: so a rollover is auditable rather than a silent reset to zero.
    previous_period: str | None = None
    previous_consumed: int | None = None

    @property
    def remaining(self) -> int:
        return max(0, self.budget - self.consumed)

    def to_body(self) -> dict[str, Any]:
        return {
            "version": _LEDGER_VERSION,
            "period": self.period,
            "consumed": self.consumed,
            "budget": self.budget,
            "previous_period": self.previous_period,
            "previous_consumed": self.previous_consumed,
        }


def _digest(body: Mapping[str, Any]) -> str:
    payload = json.dumps(
        body, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class MonthlyReadCostGuard:
    """Hard, persisted, fail-closed monthly read counter.

    Usage is deliberately awkward in one specific way: you cannot ask "how many
    are left?" and then spend that many. You call :meth:`reserve` for each unit
    of consumption, and it either durably records the reservation and returns,
    or it raises. There is no path that returns a boolean the caller can ignore.
    """

    def __init__(
        self,
        ledger_path: str | os.PathLike[str],
        budget: CostBudget,
        *,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        if budget is None:  # defensive: a None budget must never mean "off"
            raise BudgetNotConfiguredError(
                "MonthlyReadCostGuard requires an explicit CostBudget"
            )
        if not isinstance(budget, CostBudget):
            raise BudgetNotConfiguredError(
                "budget must be a CostBudget; a bare integer bypasses the "
                "validation that makes a cap explicit"
            )
        self._path = Path(ledger_path)
        self._budget = budget
        self._now = now

    # -- reading ------------------------------------------------------------

    @property
    def budget(self) -> CostBudget:
        return self._budget

    @property
    def ledger_path(self) -> Path:
        return self._path

    def read_state(self) -> CostLedgerState:
        """Return the current ledger state, or raise. Never returns a guess."""

        period = current_period(self._now())
        if not self._path.exists():
            # A fresh ledger is the one benign absence: nothing has been spent
            # because nothing has been recorded AND nothing has been read. It
            # is materialised immediately so that subsequent unreadability is
            # unambiguous.
            state = CostLedgerState(
                period=period, consumed=0, budget=self._budget.max_reads_per_month
            )
            self._write_state(state)
            return state

        try:
            raw = self._path.read_bytes()
        except OSError as exc:
            raise CounterUnreadableError(
                f"cost ledger at {self._path} could not be read: {exc}"
            ) from exc

        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CounterUnreadableError(
                f"cost ledger at {self._path} is not readable JSON: {exc}"
            ) from exc

        if not isinstance(envelope, dict):
            raise CounterUnreadableError("cost ledger is not an object")

        body = envelope.get("body")
        recorded_digest = envelope.get("digest")
        if not isinstance(body, dict) or not isinstance(recorded_digest, str):
            raise CounterUnreadableError(
                "cost ledger is missing its body or its integrity digest"
            )
        if _digest(body) != recorded_digest:
            raise CounterUnreadableError(
                "cost ledger failed its integrity check; the recorded spend "
                "cannot be trusted, so no further reads are permitted"
            )
        if body.get("version") != _LEDGER_VERSION:
            raise CounterUnreadableError(
                f"cost ledger version {body.get('version')!r} is not "
                f"{_LEDGER_VERSION!r}"
            )

        try:
            stored_period = str(body["period"])
            consumed = int(body["consumed"])
            stored_budget = int(body["budget"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CounterUnreadableError(
                f"cost ledger fields are malformed: {exc}"
            ) from exc

        if consumed < 0:
            raise CounterUnreadableError("cost ledger consumed count is negative")

        if stored_period == period:
            return CostLedgerState(
                period=period,
                consumed=consumed,
                budget=stored_budget,
                previous_period=body.get("previous_period"),
                previous_consumed=body.get("previous_consumed"),
            )

        if period < stored_period:
            raise PeriodRegressionError(
                f"observed period {period} precedes the stored period "
                f"{stored_period}; the budget is NOT reset by a clock moving "
                "backwards"
            )

        # Strictly forward: roll over, carrying the closed period's final count.
        rolled = CostLedgerState(
            period=period,
            consumed=0,
            budget=self._budget.max_reads_per_month,
            previous_period=stored_period,
            previous_consumed=consumed,
        )
        self._write_state(rolled)
        return rolled

    def remaining(self) -> int:
        """Remaining reads in the current period. Raises if unreadable."""

        state = self.read_state()
        return max(0, min(state.budget, self._budget.max_reads_per_month) - state.consumed)

    # -- spending -----------------------------------------------------------

    def reserve(self, units: int = 1) -> CostLedgerState:
        """Durably record ``units`` of consumption, or raise.

        Called BEFORE the read it pays for. Returns the post-increment state.
        """

        if not isinstance(units, int) or isinstance(units, bool) or units < 1:
            raise CostGuardError("reserve() takes a positive integer count")

        state = self.read_state()
        effective_budget = min(state.budget, self._budget.max_reads_per_month)
        if state.consumed + units > effective_budget:
            raise BudgetExhaustedError(
                f"monthly read budget exhausted for {state.period}: "
                f"{state.consumed}/{effective_budget} consumed, {units} more "
                "requested; the collector stops rather than overspending"
            )

        updated = CostLedgerState(
            period=state.period,
            consumed=state.consumed + units,
            budget=effective_budget,
            previous_period=state.previous_period,
            previous_consumed=state.previous_consumed,
        )
        self._write_state(updated)
        return updated

    def assert_startable(self) -> CostLedgerState:
        """Pre-flight check. A collector calls this before its first connect.

        Raises if the budget is absent, the ledger is unreadable, or the period
        is already exhausted — so an over-budget collector never opens a socket
        at all.
        """

        state = self.read_state()
        if state.consumed >= min(state.budget, self._budget.max_reads_per_month):
            raise BudgetExhaustedError(
                f"monthly read budget for {state.period} is already exhausted "
                f"({state.consumed}); refusing to start"
            )
        return state

    # -- durability ---------------------------------------------------------

    def _write_state(self, state: CostLedgerState) -> None:
        body = state.to_body()
        envelope = {"body": body, "digest": _digest(body)}
        payload = json.dumps(
            envelope, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")

        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic replace: a torn write must never be able to present as a
            # smaller consumed count.
            fd, tmp_name = tempfile.mkstemp(
                dir=str(self._path.parent), prefix=".cost-ledger-", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_name, self._path)
            except BaseException:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
            dir_fd = os.open(str(self._path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError as exc:
            raise CounterUnwritableError(
                f"cost ledger at {self._path} could not be durably written "
                f"({exc}); the read it would have paid for is refused"
            ) from exc
