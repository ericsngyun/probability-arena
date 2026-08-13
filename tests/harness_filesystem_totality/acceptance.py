"""The per-cell acceptance rule: what counts as a TOTAL response.

Per the milestone: within a bounded wall clock, every cell must produce
exactly one of VALID / INVALID / RECOVERY_REQUIRED / an explicit diagnostic
result -- and never a hang, an OOM, an uncaught traceback, a silent skip, or
a fatal-to-benign downgrade.

`verify_segment`, `verify_archive` and `head_state` are documented in
`app/realtime/segment.py` / `archive_head.py` as classifiers that "do not
raise" -- ANY exception out of those three is itself the defect being
tested for, not an acceptable "diagnostic result". Every other entry point
here is documented to raise a TYPED error as its diagnostic result (e.g.
`ArchiveNotInitializedError`, `HeadRecoveryRequired`, `SegmentError`), so a
raise is acceptable for them PROVIDED the exception is one of the archive's
own typed error classes -- a bare `KeyError`/`TypeError`/`OSError`/
`AttributeError` escaping is an uncaught-traceback defect regardless of
which entry point it came from.
"""

from __future__ import annotations

NEVER_RAISES = {"verify_segment", "verify_archive", "head_state"}

# (module substring, exception type name) pairs that are ACCEPTABLE for
# entry points that are documented to fail closed by raising. Anything not
# on this list -- including builtins like KeyError/TypeError/AttributeError
# -- is treated as an uncaught-traceback defect, which is exactly the
# fatal-crash class `verify_archive`'s own docstring calls out as having
# happened five times before it existed.
TYPED_EXCEPTION_ALLOWLIST = {
    ("archive_head", "ArchiveHeadError"),
    ("archive_head", "ArchiveNotInitializedError"),
    ("archive_head", "ArchiveIdentityMismatch"),
    ("archive_head", "HeadRecoveryRequired"),
    ("archive_head", "DurabilityNotProven"),
    ("archive", "ArchiveError"),
    ("segment", "SegmentError"),
    ("segment", "RecordSchemaError"),
    ("legacy_import", "LegacyImportError"),
}

_VERIFY_ARCHIVE_VERDICTS = {"VALID", "INVALID"}
_HEAD_STATES = {"NOT_INITIALIZED", "GENESIS_INVALID", "HEAD_INVALID",
                "RECOVERY_REQUIRED", "STALE_HEAD", "CURRENT"}
_SEGMENT_STATES = {"SegmentState.OPEN", "SegmentState.CLOSED",
                   "SegmentState.INVALID"}


def _typed_ok(exception_module: str | None, exception_type: str | None) -> bool:
    if not exception_module or not exception_type:
        return False
    mod = exception_module.rsplit(".", 1)[-1]
    return (mod, exception_type) in TYPED_EXCEPTION_ALLOWLIST


def classify_cell(entrypoint: str, result: dict) -> tuple:
    """(pass_bool, reason). `reason` explains a FAIL; short label on PASS."""
    kind = result["classification"]

    if kind == "TIMEOUT":
        return False, f"HUNG past {result.get('timeout_s')}s wall clock"
    if kind == "CRASHED":
        return False, (f"child process crashed/uncaught fatal, "
                       f"returncode={result.get('returncode')}: "
                       f"{result.get('stderr', '')[-300:]}")
    if kind == "PROTOCOL_VIOLATION":
        return False, ("child did not emit exactly one JSON verdict line "
                       f"(stdout={result.get('stdout', '')[-300:]!r})")

    if kind == "RAISED":
        if entrypoint in NEVER_RAISES:
            return False, (f"{entrypoint} is documented to classify without "
                           f"raising, but raised "
                           f"{result.get('exception_type')}: "
                           f"{result.get('exception_message')}")
        if _typed_ok(result.get("exception_module"), result.get("exception_type")):
            return True, f"typed raise: {result.get('exception_type')}"
        return False, (f"uncaught/untyped exception "
                       f"{result.get('exception_module')}."
                       f"{result.get('exception_type')}: "
                       f"{result.get('exception_message')}")

    assert kind == "RETURNED"
    detail = result.get("detail") or {}
    if entrypoint == "verify_archive":
        v = detail.get("verdict")
        if v not in _VERIFY_ARCHIVE_VERDICTS:
            return False, f"verify_archive returned unrecognised verdict {v!r}"
        return True, f"verdict={v}"
    if entrypoint == "head_state":
        s = detail.get("state")
        if s not in _HEAD_STATES:
            return False, f"head_state returned unrecognised state {s!r}"
        return True, f"state={s}"
    if entrypoint == "verify_segment":
        s = detail.get("state")
        if s not in _SEGMENT_STATES:
            return False, f"verify_segment returned unrecognised state {s!r}"
        return True, f"state={s}"
    return True, "returned"
