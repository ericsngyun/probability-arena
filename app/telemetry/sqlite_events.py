"""Session-scoped SQLAlchemy timing listeners + op-context (001A).

Listeners attach to the ONE ``Session`` instance a maintenance writer owns
for the duration of its operation — never to the shared engine or the
shared sessionmaker. 001A therefore adds zero callbacks to the MarketOps
hot path; shared-engine registration is explicitly deferred to 001B (after
the readiness window closes 2026-07-30).

Timing semantics (design §Timing): SQLite rollback-journal exposes no API
for exact lock hold, so the per-transaction write-lock hold is ESTIMATED as
first-DML-flush → commit/rollback end (``instrumented_estimate``), reset on
every ``after_commit``; commit duration (before_commit → after_commit) is
``exact``; lock wait derived from retry ladders is a ``derived_estimate``
floor. Listeners write to an in-process op-context only — never the DB.
"""

from __future__ import annotations

import contextlib
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import event

from app.telemetry.schema import build_event, writer_instance_id
from app.telemetry.sink import get_sink


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.isoformat().replace("+00:00", "Z")


@dataclass
class OpContext:
    """In-process measurement state for one writer operation. Plain data —
    the listeners mutate it; the emit helpers read it. Never touches a DB."""

    writer_name: str
    writer_class: str
    operation_name: str
    started_at: datetime = field(default_factory=_utcnow)
    instance_id: str = field(default_factory=writer_instance_id)
    parent_event_id: str | None = None

    # listener-maintained (per-transaction, reset on after_commit)
    first_mutation_at: datetime | None = None
    commit_started_at: datetime | None = None
    last_transaction_hold_ms: int | None = None
    last_commit_ms: int | None = None
    last_rollback_ms: int | None = None
    _commit_started_monotonic: float | None = None
    _rollback_started_monotonic: float | None = None

    provider_io_during_transaction: bool = False
    filesystem_io_during_transaction: bool = False

    def source_command(self) -> str | None:
        argv = sys.argv[1:] if sys.argv else []
        for token in argv:
            if not token.startswith("-") and not token.endswith(".py"):
                return token
        return None

    def mark_rollback_start(self) -> None:
        """Called from the writer's except-block: SQLAlchemy has no
        ``before_rollback`` hook, so rollback START is service-captured."""
        self._rollback_started_monotonic = time.monotonic()


def _make_listeners(ctx: OpContext) -> dict:
    # Every listener body is exception-guarded: these run inside SQLAlchemy's
    # commit/rollback dispatch, where an escaped exception would become a
    # writer failure — the contract says telemetry can never do that.
    def after_flush(session, flush_context) -> None:
        try:
            # first DML flush of the transaction ≈ earliest instant the
            # RESERVED write lock could be acquired (instrumented_estimate)
            if ctx.first_mutation_at is None and (
                session.new or session.dirty or session.deleted
            ):
                ctx.first_mutation_at = _utcnow()
        except Exception:  # pragma: no cover - defensive
            pass

    def before_commit(session) -> None:
        try:
            ctx.commit_started_at = _utcnow()
            ctx._commit_started_monotonic = time.monotonic()
        except Exception:  # pragma: no cover - defensive
            pass

    def after_commit(session) -> None:
        try:
            now = time.monotonic()
            if ctx._commit_started_monotonic is not None:
                ctx.last_commit_ms = int((now - ctx._commit_started_monotonic) * 1000)
            if ctx.first_mutation_at is not None:
                ctx.last_transaction_hold_ms = int(
                    (_utcnow() - ctx.first_mutation_at).total_seconds() * 1000
                )
            # per-transaction, never per-operation: reset so the next
            # committed transaction reports its own hold, not a merged span
            ctx.first_mutation_at = None
            ctx._commit_started_monotonic = None
        except Exception:  # pragma: no cover - defensive
            pass

    def after_rollback(session) -> None:
        try:
            if ctx._rollback_started_monotonic is not None:
                ctx.last_rollback_ms = int(
                    (time.monotonic() - ctx._rollback_started_monotonic) * 1000
                )
                ctx._rollback_started_monotonic = None
            ctx.first_mutation_at = None
            ctx._commit_started_monotonic = None
        except Exception:  # pragma: no cover - defensive
            pass

    return {
        "after_flush": after_flush,
        "before_commit": before_commit,
        "after_commit": after_commit,
        "after_rollback": after_rollback,
    }


@contextlib.contextmanager
def session_op_context(session, ctx: OpContext):
    """Attach the timing listeners to ONE session instance for the lifetime
    of one operation, and always detach them. Attach/detach failures are
    swallowed — telemetry can never fail the writer."""
    listeners = _make_listeners(ctx)
    attached: list[tuple[str, object]] = []
    try:
        for name, fn in listeners.items():
            try:
                event.listen(session, name, fn)
                attached.append((name, fn))
            except Exception:
                pass
        yield ctx
    finally:
        for name, fn in attached:
            with contextlib.suppress(Exception):
                event.remove(session, name, fn)


@contextlib.contextmanager
def provider_io_span(ctx: OpContext, session=None):
    """Mark provider I/O occurring while the session has a dirty/open write
    transaction. 001A writers perform none; the primitive exists (and is
    tested) for the later slices that do."""
    dirty = True
    if session is not None:
        try:
            dirty = bool(
                session.in_transaction()
                and (session.new or session.dirty or session.deleted)
            )
        except Exception:
            dirty = True
    if dirty:
        ctx.provider_io_during_transaction = True
    yield


def emit_event(ctx: OpContext, *, finished_at: datetime | None = None,
               outcome: str = "success", **fields) -> str | None:
    """Build + emit one envelope from an op-context AFTER commit or terminal
    failure handling. Returns the event_id (for parent/child correlation) or
    None. NEVER raises into the writer."""
    try:
        finished = finished_at or _utcnow()
        event_dict = build_event(
            writer_name=ctx.writer_name,
            writer_class=ctx.writer_class,
            operation_name=fields.pop("operation_name", ctx.operation_name),
            started_at=_iso(fields.pop("started_at", ctx.started_at)),
            finished_at=_iso(finished),
            outcome=outcome,
            writer_instance_id=ctx.instance_id,
            parent_event_id=fields.pop("parent_event_id", ctx.parent_event_id),
            source_command=fields.pop("source_command", ctx.source_command()),
            provider_io_during_transaction=fields.pop(
                "provider_io_during_transaction",
                ctx.provider_io_during_transaction),
            filesystem_io_during_transaction=fields.pop(
                "filesystem_io_during_transaction",
                ctx.filesystem_io_during_transaction),
            **fields,
        )
        if get_sink().emit(event_dict):
            return event_dict["event_id"]
        return None
    except Exception:
        return None


def sample_gauges() -> dict:
    """Best-effort database/filesystem gauges for maintenance writers.
    Read-only: file stat + statvfs; no DB connection, no pragma here."""
    gauges: dict = {}
    try:
        from sqlalchemy.engine.url import make_url

        from app.config import get_settings

        url = make_url(get_settings().database_url)
        if url.get_backend_name() == "sqlite" and url.database:
            db_path = url.database
            if os.path.exists(db_path):
                gauges["database_bytes"] = os.path.getsize(db_path)
                vfs = os.statvfs(os.path.dirname(os.path.abspath(db_path)))
                gauges["filesystem_free_bytes"] = vfs.f_bavail * vfs.f_frsize
    except Exception:
        pass
    return gauges
