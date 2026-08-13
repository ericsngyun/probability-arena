"""Every filesystem shape the matrix can plant at an artifact path.

Each shape function REMOVES whatever is currently at `target` and replaces it
with the requested shape. `kind` is `"file"` or `"dir"` and states what a
*healthy* artifact at that path looks like, which some shapes need (e.g. a
symlink-to-file shape copies the ORIGINAL bytes to a side location and links
to that, so a healthy reader that is not fooled by any shape still sees the
same content).
"""

from __future__ import annotations

import gzip
import os
import socket
import stat
from pathlib import Path

DEV_ZERO = Path("/dev/zero")

# Every shape name this module knows how to plant. Kept as a tuple, not a
# set, so the matrix can iterate in a stable, reproducible order.
ALL_SHAPES = (
    "regular",
    "missing",
    "dir_in_place_of_file",
    "file_in_place_of_dir",
    "symlink_to_file",
    "symlink_to_dir",
    "broken_symlink",
    "fifo",
    "unix_socket",
    "dev_zero_symlink",
    "mode_000_file",
    "execute_only_dir",
    "mode_000_dir",
    "unreadable_file",           # alias of mode_000_file, kept distinct in
                                  # reporting because the milestone names both
    "malformed_json",
    "truncated_gzip",
    "corrupt_gzip",
)

# Which shapes make sense for a FILE artifact vs. a DIRECTORY artifact.
FILE_SHAPES = (
    "regular", "missing", "dir_in_place_of_file", "symlink_to_file",
    "broken_symlink", "fifo", "unix_socket", "dev_zero_symlink",
    "mode_000_file", "unreadable_file", "malformed_json", "truncated_gzip",
    "corrupt_gzip",
)
GZIP_ONLY_SHAPES = ("truncated_gzip", "corrupt_gzip")
JSON_ONLY_SHAPES = ("malformed_json",)
DIR_SHAPES = (
    "regular", "missing", "file_in_place_of_dir", "symlink_to_dir",
    "broken_symlink", "fifo", "execute_only_dir", "mode_000_dir",
)


def _rm(target: Path) -> None:
    """Remove whatever is at `target`, restoring permissions first if needed."""
    target = Path(target)
    try:
        st = target.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode):
        _chmod_tree_rw(target)
        import shutil
        shutil.rmtree(target, ignore_errors=True)
    else:
        try:
            os.chmod(target, 0o700)
        except OSError:
            pass
        try:
            target.unlink()
        except OSError:
            pass


def _chmod_tree_rw(root: Path) -> None:
    """Restore rwx on `root` and everything under it, INCLUDING an
    execute-only or mode-000 `root` itself.

    `os.walk` only yields `(dirpath, dirnames, filenames)` for a directory
    it could already `scandir()` -- so a directory that is itself mode 000
    or 0o111 is invisible to a walk that starts INSIDE it, and a plain
    `os.walk(root)` never gets the chance to loosen `root`'s own mode before
    trying (and failing) to list it. This chmods `root` unconditionally
    before walking, and then -- because `os.walk` with `topdown=True`
    (the default) hands over each directory's `dirnames` BEFORE attempting
    to descend into them -- chmods every child directory the moment its
    NAME is known, which is before `os.walk` ever tries to `scandir()` it.
    Symlinks are skipped: `os.chmod` on a symlink to `/dev/zero` or a
    quarantined side directory would touch the TARGET, not this tree.
    """
    root = Path(root)
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        for d in dirnames:
            p = os.path.join(dirpath, d)
            if os.path.islink(p):
                continue
            try:
                os.chmod(p, 0o700)
            except OSError:
                pass
        for f in filenames:
            p = os.path.join(dirpath, f)
            if os.path.islink(p):
                continue
            try:
                os.chmod(p, 0o600)
            except OSError:
                pass


def cleanup_permissions(root: Path) -> None:
    """Restore rwx everywhere under `root` so pytest teardown can delete it."""
    _chmod_tree_rw(root)


def _write_regular(target: Path, content: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


def _copy_dir(src: Path, dst: Path) -> None:
    import shutil
    shutil.copytree(src, dst)


def place_file_shape(target: Path, shape: str, *, original_bytes: bytes,
                     side_dir: Path) -> None:
    """Plant `shape` at `target`, a path where a FILE artifact normally lives.

    `original_bytes` is the healthy content (used by shapes that legitimately
    still resolve to real content, e.g. symlink-to-file). `side_dir` is a
    scratch directory OUTSIDE the archive root used to stage symlink targets,
    sockets, etc.
    """
    target = Path(target)
    side_dir = Path(side_dir)
    side_dir.mkdir(parents=True, exist_ok=True)
    _rm(target)
    target.parent.mkdir(parents=True, exist_ok=True)

    if shape == "regular":
        _write_regular(target, original_bytes)
    elif shape == "missing":
        pass                          # already removed; nothing to create
    elif shape == "dir_in_place_of_file":
        target.mkdir(parents=True)
    elif shape == "symlink_to_file":
        real = side_dir / f"{target.name}.real"
        real.write_bytes(original_bytes)
        target.symlink_to(real)
    elif shape == "broken_symlink":
        target.symlink_to(side_dir / "does-not-exist-at-all")
    elif shape == "fifo":
        os.mkfifo(target)
    elif shape == "unix_socket":
        # A socket must itself be the node the reader stats, and sockets
        # cannot be hardlinked or created at an arbitrary depth reliably
        # (AF_UNIX `sun_path` is capped at ~104 bytes on macOS / 108 on
        # Linux) -- so this binds DIRECTLY at `target`. Callers keep the
        # archive root short (see `archive_fixture.SHORT_ROOT_PREFIX`) so
        # every artifact path stays well under that limit.
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.bind(str(target))
        finally:
            s.close()
    elif shape == "dev_zero_symlink":
        if not DEV_ZERO.exists():
            raise RuntimeError("/dev/zero is not available on this host")
        target.symlink_to(DEV_ZERO)
    elif shape == "mode_000_file" or shape == "unreadable_file":
        target.write_bytes(original_bytes)
        os.chmod(target, 0o000)
    elif shape == "malformed_json":
        target.write_bytes(b"{ this is not json, it is a probe :::")
    elif shape == "truncated_gzip":
        real_gz = gzip.compress(original_bytes or b'{"probe": true}\n')
        target.write_bytes(real_gz[: max(1, len(real_gz) // 2)])
    elif shape == "corrupt_gzip":
        real_gz = bytearray(gzip.compress(original_bytes or b'{"probe": true}\n'))
        # Flip bytes past the header so the stream is syntactically gzip but
        # semantically garbage (bad CRC / bad deflate stream), not just short.
        for i in range(10, min(len(real_gz), 40)):
            real_gz[i] ^= 0xFF
        target.write_bytes(bytes(real_gz))
    else:
        raise ValueError(f"unknown file shape {shape!r}")


def place_dir_shape(target: Path, shape: str, *, template: Path | None,
                    side_dir: Path) -> None:
    """Plant `shape` at `target`, a path where a DIRECTORY artifact lives.

    `template`, if given, is a healthy directory (with real children) that
    `symlink_to_dir` links to instead of `target` holding real children
    directly.
    """
    target = Path(target)
    side_dir = Path(side_dir)
    side_dir.mkdir(parents=True, exist_ok=True)
    _rm(target)
    target.parent.mkdir(parents=True, exist_ok=True)

    if shape == "regular":
        if template is not None and template.exists():
            _copy_dir(template, target)
        else:
            target.mkdir(parents=True)
    elif shape == "missing":
        pass
    elif shape == "file_in_place_of_dir":
        target.write_bytes(b"a file where a directory belongs\n")
    elif shape == "symlink_to_dir":
        real = side_dir / f"{target.name}.realdir"
        if real.exists():
            import shutil
            shutil.rmtree(real)
        if template is not None and template.exists():
            _copy_dir(template, real)
        else:
            real.mkdir(parents=True)
        target.symlink_to(real, target_is_directory=True)
    elif shape == "broken_symlink":
        target.symlink_to(side_dir / "does-not-exist-at-all-dir")
    elif shape == "fifo":
        os.mkfifo(target)
    elif shape == "execute_only_dir":
        if template is not None and template.exists():
            _copy_dir(template, target)
        else:
            target.mkdir(parents=True)
        os.chmod(target, 0o111)
    elif shape == "mode_000_dir":
        if template is not None and template.exists():
            _copy_dir(template, target)
        else:
            target.mkdir(parents=True)
        os.chmod(target, 0o000)
    else:
        raise ValueError(f"unknown dir shape {shape!r}")
