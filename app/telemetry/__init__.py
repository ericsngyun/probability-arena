"""SQLITE-LOCK-TELEMETRY-001A — measurement-only SQLite writer telemetry.

Nonblocking, append-only, **non-SQLite** JSONL telemetry for shared-database
writers, per ``docs/SQLITE_LOCK_TELEMETRY_DESIGN_2026_07.md``. The 001A slice
covers primitives + the file sink and instruments the tick-aggregation writer
and the backup reader via session-scoped listeners (``sqlite_events``).
GATE2-WRITER-TELEMETRY-001 adds ``writer_pass`` — one PASS-level record per
governed run of the two Solana writers (``crypto_tape``,
``crypto_horizon_observe``), emitted after the pass has finished all of its
own commits and therefore attaching no listeners to anything.

Nothing here attaches to the shared engine, changes a transaction boundary,
retry ladder, journal mode, schedule, or pragma, and nothing here ever writes
to the application database.

Telemetry is ``durable_but_nonblocking``: a telemetry failure can never fail,
slow, or roll back a writer. Measurement only — never a trading surface of
any kind (see ``docs/SAFETY_BOUNDARIES.md``).
"""

from app.telemetry.schema import build_event, validate_event  # noqa: F401
from app.telemetry.sink import TelemetrySink, get_sink  # noqa: F401
