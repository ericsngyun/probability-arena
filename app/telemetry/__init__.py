"""SQLITE-LOCK-TELEMETRY-001A — measurement-only SQLite writer telemetry.

Nonblocking, append-only, **non-SQLite** JSONL telemetry for shared-database
writers, per ``docs/SQLITE_LOCK_TELEMETRY_DESIGN_2026_07.md``. This slice
(001A) covers primitives + the file sink and instruments ONLY the
tick-aggregation writer and the backup reader. It attaches nothing to the
shared engine, changes no transaction boundary, retry ladder, journal mode,
schedule, or pragma, and never writes to the application database.

Telemetry is ``durable_but_nonblocking``: a telemetry failure can never fail,
slow, or roll back a writer. Measurement only — never a trading surface of
any kind (see ``docs/SAFETY_BOUNDARIES.md``).
"""

from app.telemetry.schema import build_event, validate_event  # noqa: F401
from app.telemetry.sink import TelemetrySink, get_sink  # noqa: F401
