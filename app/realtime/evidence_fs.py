"""KALSHI-ARCHIVE-CORE-REMEDIATION-002 A1 — the ONE evidence-filesystem
abstraction.

Eight prior remediation rounds each patched one call site that reproduced one
symptom, and the class kept reappearing at the next call site because
`_presence` and `containment_reason` lived in `segment.py` and no production
reader in `archive_head.py` or `archive.py` ever called either one. This
module is the fix for THAT: every canonical archive module (`archive.py`,
`archive_head.py`, `segment.py`, `legacy_import.py`) routes its evidence-path
filesystem access through the primitives here, so a defect fixed once here is
fixed everywhere it is reachable, and a NEW raw filesystem call anywhere else
in those modules is exactly what `tests/harness_filesystem_totality/
ast_audit.py`'s static sweep exists to catch.

Seven primitives, each TOTAL (never raises, never blocks past a syscall that
itself cannot block, never allocates without bound):

    presence(path)                  -- (True/False/None, reason) by stat kind
    containment_reason(root, target)-- is `target` bounded by `root`?
    assert_contained(root, target)  -- containment as an exception
    safe_enumerate(dir, pattern)    -- honest directory listing (EACCES included)
    bounded_read(path, ...)         -- whole-file read, capped, non-regular refused
    open_bounded_gzip(path, ...)    -- gzip reader over a bounded, regular-file read
    is_regular_file(path)           -- True only for a real, resolvable regular file

The property that closes the class rather than the instance: NONE of these
ever call `open()`, `read_bytes()` or `gzip.open()` on a path before first
proving -- by `stat`, which cannot block on a FIFO/socket/device the way
`open()` can -- that the final target is a regular file. A FIFO, a socket, a
character device, and a symlink chain ending at any of those are refused
before the syscall that could hang or read without bound is ever reached.
"""

from __future__ import annotations

import fnmatch
import os
import stat as _stat
from pathlib import Path

# Generous relative to any real hourly segment or head artifact (head
# artifacts are a few hundred bytes; segments rotate well under this by
# policy), and small enough that a device node lying about its size (or an
# ordinary file that grew unexpectedly large) cannot force an unbounded
# allocation. Not a correctness bound for legitimate evidence -- a safety
# bound against the unbounded-read class (`/dev/zero`, a runaway log).
DEFAULT_MAX_READ_BYTES = 1 << 30  # 1 GiB


class EvidenceAccessError(OSError):
    """Typed failure from a bounded-read/open primitive in this module.

    Raised only by the primitives that are documented to raise
    (`assert_contained`, `open_bounded_gzip`); the presence/read primitives
    that classify evidence for a verdict (`presence`, `bounded_read`,
    `safe_enumerate`) never raise -- a verdict function cannot afford to.
    Subclasses `OSError` so a caller that already catches `OSError` around a
    raw filesystem call keeps working unchanged after routing through here.
    """


def presence(path) -> tuple:
    """(present, reason). Never raises -- a verdict function cannot afford to.

    `present` is `True` (a regular file, directory, or symlink is there by
    name -- `os.lstat` does not follow the final component, so this answers
    "is there a filesystem entry here", not "does it resolve to something
    readable"), `False` (nothing there -- `ENOENT`/`ENOTDIR`), or `None`
    (the answer could not be determined at all -- permission denied, a
    directory in the path is unreadable, etc; `reason` carries why).

    `Path.exists()` propagates `EACCES`; `os.path.lexists` swallows every
    `OSError` internally and answers `False`, which conflates "gone" with
    "denied" -- exactly the distinction Orphan/Genesis semantics depend on
    (a `False` from `os.path.lexists` on a permission-denied genesis reads
    identically to a genuinely deleted one). Neither is total on its own;
    this is.
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False, None
    except OSError as exc:
        return None, f"{Path(path).name} could not be examined: {exc!r}"
    if not (_stat.S_ISREG(st.st_mode) or _stat.S_ISDIR(st.st_mode)
            or _stat.S_ISLNK(st.st_mode)):
        # A FIFO, socket or device node at an evidence path answers "present"
        # to `lstat` and then blocks a reader FOREVER if anything downstream
        # ever calls `open()`/`read_bytes()` on it. Refusing it BY NAME here,
        # before any open is attempted, is what makes every primitive below
        # total instead of merely "usually fast".
        return None, (f"{Path(path).name} is not a regular file, directory "
                      f"or symlink (mode {_stat.S_IFMT(st.st_mode):#o})")
    return True, None


def assert_contained(root: Path, target: Path) -> Path:
    """Every component between root and target must be a real directory.

    `Path(root).resolve()` resolves the ROOT, not its children, and
    `mkdir(parents=True, exist_ok=True)` happily traverses a symlinked
    `env=<name>` component. A planted symlink there put every record, every
    manifest and the authoritative head outside the configured root while
    verification still reported VALID -- the archive root stopped bounding
    the evidence.

    The target is matched against the root BOTH as given and as resolved. A
    caller that passes an unresolved root (`/tmp/...` on macOS, or the kind
    of symlinked `BACKUP_DIR` an operator actually configures) previously got
    a bare `ValueError` out of `relative_to`, raised from the orphan repair
    path -- the one route out of a crash between manifest publish and head
    commit.

    This is a path-based check, not `openat` on a pinned dirfd: a component
    swapped between here and the subsequent open is NOT prevented.
    Verification catches such a swap afterwards, so the property is
    tamper-EVIDENT, not race-free, and it is stated that way deliberately.
    """
    root_raw = Path(root)
    root_res = root_raw.resolve()
    target = Path(target)
    for candidate, base in ((target, root_raw), (target, root_res),
                            (target.resolve() if target.exists() else target,
                             root_res)):
        try:
            parts = candidate.relative_to(base).parts
            break
        except ValueError:
            continue
    else:
        raise EvidenceAccessError(
            f"{target} is outside the archive root {root_raw}")
    if ".." in parts:
        # `pathlib` does not collapse `..`, and the component walk below only
        # rejects symlinks and non-directories -- `..` is neither, so
        # `root/a/../../evil` was reported as contained.
        raise EvidenceAccessError(
            f"{target} contains a '..' component; containment is decided by "
            "the path as written, so an unresolved parent reference is "
            "refused")
    current = root_res
    last = len(parts) - 1
    for i, part in enumerate(parts):
        current = current / part
        if current.is_symlink():
            raise EvidenceAccessError(
                f"{current} is a symlink; no path component between the "
                "archive root and its segments may be a link, or the root "
                "stops bounding the evidence")
        if i != last and current.exists() and not current.is_dir():
            raise EvidenceAccessError(f"{current} is not a directory")
    return target


def containment_reason(root, target) -> str | None:
    """`assert_contained` as a verification reason rather than an exception.

    Total by contract: `assert_contained` stats the target (its candidate
    tuple eagerly calls `target.exists()`), so it can raise `EACCES` for
    exactly the reason the caller's guard was meant to stop. Every caller of
    THIS function gets that turned into a reason string instead.
    """
    try:
        assert_contained(root, target)
    except EvidenceAccessError as exc:
        return str(exc)
    except OSError as exc:
        return f"{Path(target).name} could not be examined: {exc!r}"
    return None


def safe_enumerate(directory, pattern: str = "*") -> tuple:
    """(children, error). Honest directory listing.

    `Path.glob()` swallows the `PermissionError` `os.scandir` raises
    internally and returns `[]` -- indistinguishable from "this directory is
    genuinely empty". An execute-only directory (`0o111`: traversable by
    name, not listable) then makes every enumeration-based accounting
    silently zero out, with no diagnostic that anything was missed.
    `os.scandir` is used directly here specifically because it is honest:
    it raises.
    """
    directory = Path(directory)
    try:
        with os.scandir(directory) as it:
            names = sorted(e.name for e in it)
    except OSError as exc:
        return [], f"{directory.name} could not be enumerated: {exc!r}"
    return [directory / n for n in names if fnmatch.fnmatch(n, pattern)], None


def is_regular_file(path) -> bool:
    """True only if `path`, resolved through any symlink chain, is an
    ordinary regular file. False for missing, a directory, a FIFO, a socket,
    a device node, or anything `os.stat` cannot examine (permission denied
    included -- callers that must distinguish "refused" from "not a regular
    file" should call `bounded_read`/`presence` instead, which carry a
    reason; this is the boolean gate used where only the yes/no answer
    matters, e.g. before an unconditional enumeration filter).
    """
    try:
        st = os.stat(path)
    except OSError:
        return False
    return _stat.S_ISREG(st.st_mode)


def bounded_read(path, *, max_bytes: int = DEFAULT_MAX_READ_BYTES) -> tuple:
    """(data, reason). `data` is `None` iff `reason` is not `None`.

    Refuses anything that does not resolve (through at most the ordinary
    symlink-following `os.stat` does) to a regular file BEFORE calling
    `open()` -- so a FIFO, a socket, a device node, or a symlink to any of
    those can never reach the `open()`/`read()` call that would block or
    read forever. The declared size is checked before a single byte is read
    (so a device node lying about its size, or an ordinary file that grew
    past the bound, is refused up front), and the actual bytes read are
    checked against the same bound again (so a file that grows WHILE it is
    being read cannot exceed it either).
    """
    path = Path(path)
    try:
        st = os.stat(path)
    except OSError as exc:
        return None, f"{path.name} could not be examined: {exc!r}"
    if not _stat.S_ISREG(st.st_mode):
        return None, (f"{path.name} is not a regular file "
                      f"(mode {_stat.S_IFMT(st.st_mode):#o})")
    if st.st_size > max_bytes:
        return None, (f"{path.name} is {st.st_size} bytes, over the "
                      f"{max_bytes}-byte bound for a canonical evidence read")
    try:
        with open(path, "rb") as fh:
            data = fh.read(max_bytes + 1)
    except OSError as exc:
        return None, f"{path.name} could not be read: {exc!r}"
    if len(data) > max_bytes:
        return None, (f"{path.name} grew past the {max_bytes}-byte bound "
                      "while it was being read")
    return data, None


def open_bounded_gzip(path, *, max_bytes: int = DEFAULT_MAX_READ_BYTES):
    """A `gzip.GzipFile` over bytes already proven regular-file-and-bounded.

    Raises `EvidenceAccessError` (never a raw `OSError`) if the source is not
    a boundedly-readable regular file -- callers that need a total,
    non-raising classification should call `bounded_read` directly and
    branch on `reason` themselves instead.

    Deliberately reads the whole (bounded) source into memory first rather
    than streaming through `gzip.open(path)` directly: `gzip.open` on a path
    defers the `open()`/first `read()` to whenever the caller first reads
    from the returned handle, which reintroduces exactly the unguarded
    blocking/unbounded-read call this module exists to remove. Reading
    through `bounded_read` first means the syscall that could hang or read
    forever never happens without the regular-file-and-size check in front
    of it.
    """
    import gzip
    import io

    data, reason = bounded_read(path, max_bytes=max_bytes)
    if reason is not None:
        raise EvidenceAccessError(reason)
    return gzip.GzipFile(fileobj=io.BytesIO(data))
