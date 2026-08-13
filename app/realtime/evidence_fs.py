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
import hashlib
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


def _open_verified_fd(path):
    """(fd, stat_result, reason). `fd` and `stat_result` are `None` iff
    `reason` is not `None`; a returned `fd` is the caller's to `os.close`.

    THE PRIMITIVE THAT CLOSES BOTH THE SHAPE AND THE RACE: `bounded_read`
    used to `os.stat(path)` (check) and then separately `open(path, "rb")`
    (use) -- a plain check-then-use, so a component swapped between the two
    calls (a regular file replaced by a symlink to a FIFO, for instance)
    made the stat's regular-file proof say nothing about what `open()`
    actually opened. `open(path)`/`Path.open()` also follow a symlink at the
    final component even when every path component up to it was already
    proven safe by containment, so a swap at the LAST component specifically
    bypassed a caller that had done everything else right.

    `os.open(..., O_NOFOLLOW)` refuses a symlink at the final component
    atomically as part of the same syscall that acquires the fd -- there is
    no window between "look at the final component" and "open what's
    there" for anything to swap. `O_NONBLOCK` means the open itself cannot
    block even if the previous check somehow raced against a FIFO/device
    swap (a regular file is unaffected by the flag; only a FIFO opened for
    read would otherwise block until a writer attaches). The type proof
    then runs `fstat` ON THE FD, not the path -- once a real regular-file fd
    is obtained, nothing that happens to the name afterward (delete,
    rename, re-symlink) can change what this reads, because reads through
    an already-open fd are bound to the inode, not the path.
    """
    path = Path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
                     | os.O_CLOEXEC)
    except OSError as exc:
        return None, None, f"{path.name} could not be opened: {exc!r}"
    try:
        st = os.fstat(fd)
    except OSError as exc:
        os.close(fd)
        return None, None, f"{path.name} could not be examined: {exc!r}"
    if not _stat.S_ISREG(st.st_mode):
        os.close(fd)
        return None, None, (f"{path.name} is not a regular file "
                            f"(mode {_stat.S_IFMT(st.st_mode):#o})")
    return fd, st, None


def bounded_read(path, *, max_bytes: int = DEFAULT_MAX_READ_BYTES) -> tuple:
    """(data, reason). `data` is `None` iff `reason` is not `None`.

    Routed entirely through `_open_verified_fd` -- see that primitive's
    docstring for why an fd-based open-then-fstat closes the check-then-use
    race that a stat-then-open shape cannot. The declared size is checked
    before a single byte is read (so a device node lying about its size, or
    an ordinary file that grew past the bound, is refused up front), and the
    actual bytes read are checked against the same bound again (so a file
    that grows WHILE it is being read cannot exceed it either).
    """
    path = Path(path)
    fd, st, reason = _open_verified_fd(path)
    if reason is not None:
        return None, reason
    try:
        if st.st_size > max_bytes:
            return None, (f"{path.name} is {st.st_size} bytes, over the "
                          f"{max_bytes}-byte bound for a canonical evidence read")
        chunks = []
        total = 0
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                return None, (f"{path.name} grew past the {max_bytes}-byte "
                              "bound while it was being read")
            chunks.append(chunk)
        return b"".join(chunks), None
    except OSError as exc:
        return None, f"{path.name} could not be read: {exc!r}"
    finally:
        os.close(fd)


def stat_and_sha256_bounded(path, *, max_bytes: int = DEFAULT_MAX_READ_BYTES) -> tuple:
    """(size, hex_digest, reason). `size` and `hex_digest` are `None` iff
    `reason` is not `None`.

    A caller that needs BOTH the size and the digest of the same evidence
    file (`verify_segment` checks `event_file_size_bytes` and
    `event_file_sha256` against the same manifest) used to get them from two
    SEPARATE filesystem calls (`Path.stat()` then `file_sha256`'s own
    `open()`) -- two independent opportunities for the file under the name
    to have changed between them, and, before this module, no shared
    containment or regular-file proof between the two at all. This opens
    ONCE, through `_open_verified_fd`, and reads the size from the SAME fd's
    `fstat` that then gets hashed -- the size and the digest are provably of
    the identical bytes.
    """
    path = Path(path)
    fd, st, reason = _open_verified_fd(path)
    if reason is not None:
        return None, None, reason
    try:
        if st.st_size > max_bytes:
            return None, None, (f"{path.name} is {st.st_size} bytes, over the "
                                f"{max_bytes}-byte bound for a canonical evidence read")
        h = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                return None, None, (f"{path.name} grew past the {max_bytes}-byte "
                                    "bound while it was being hashed")
            h.update(chunk)
        if total != st.st_size:
            return None, None, (f"{path.name} was {st.st_size} bytes at open "
                                f"but {total} bytes were read (grew or shrank "
                                "while being hashed)")
        return st.st_size, h.hexdigest(), None
    except OSError as exc:
        return None, None, f"{path.name} could not be read: {exc!r}"
    finally:
        os.close(fd)


def sha256_bounded(path, *, max_bytes: int = DEFAULT_MAX_READ_BYTES) -> tuple:
    """(hex_digest, reason). The canonical replacement for a raw `open()` +
    `hashlib.sha256()` loop: hashes a STREAM read through the same
    fd-based, TOCTOU-free, size-bounded primitive as `bounded_read`, without
    ever materializing the whole file in memory (unlike `bounded_read`,
    which a hasher does not need to -- a segment's events file is exactly
    the artifact `bounded_read`'s own DEFAULT_MAX_READ_BYTES docstring notes
    can legitimately approach the bound).
    """
    path = Path(path)
    fd, st, reason = _open_verified_fd(path)
    if reason is not None:
        return None, reason
    try:
        if st.st_size > max_bytes:
            return None, (f"{path.name} is {st.st_size} bytes, over the "
                          f"{max_bytes}-byte bound for a canonical evidence hash")
        h = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                return None, (f"{path.name} grew past the {max_bytes}-byte "
                              "bound while it was being hashed")
            h.update(chunk)
        return h.hexdigest(), None
    except OSError as exc:
        return None, f"{path.name} could not be read: {exc!r}"
    finally:
        os.close(fd)


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
