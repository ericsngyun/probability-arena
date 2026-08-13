"""A2 — the red-team corpus: production-shaped snippets a filesystem-
totality guard must discriminate correctly in BOTH directions.

Every `UNSAFE` entry is a call shape that reaches a blocking/unbounded
filesystem primitive without going through `app/realtime/evidence_fs.py`'s
total primitives; a guard that does not flag it has a blind spot exactly
like the one this milestone exists to close (`file_sha256`'s raw `open()`).

Every `SAFE` entry is a KNOWN-GOOD call shape -- through `evidence_fs`'s own
primitives -- that a guard must NOT flag, or the guard is useless in
practice: a guard that flags its own approved abstraction trains reviewers
to ignore it.

Each entry carries the literal milestone requirement it satisfies, so the
corpus's coverage of the milestone text is auditable line-by-line rather
than asserted.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CorpusEntry:
    id: str
    requirement: str
    code: str
    expect_flagged: bool


UNSAFE: tuple[CorpusEntry, ...] = (
    CorpusEntry("open_call", "`open(path)`", """
path = "/tmp/x"
open(path)
""", True),
    CorpusEntry("path_open_ctor", "`Path(path).open()`", """
from pathlib import Path
path = "/tmp/x"
Path(path).open()
""", True),
    CorpusEntry("path_var_open", "`path.open()`", """
from pathlib import Path
path = Path("/tmp/x")
path.open()
""", True),
    CorpusEntry("path_read_text", "`Path(path).read_text()`", """
from pathlib import Path
path = "/tmp/x"
Path(path).read_text()
""", True),
    CorpusEntry("path_read_bytes", "`Path(path).read_bytes()`", """
from pathlib import Path
path = "/tmp/x"
Path(path).read_bytes()
""", True),
    CorpusEntry("gzip_open", "`gzip.open(path)`", """
import gzip
path = "/tmp/x"
gzip.open(path)
""", True),
    CorpusEntry("gzip_gzipfile", "`gzip.GzipFile(path)`", """
import gzip
path = "/tmp/x"
gzip.GzipFile(path)
""", True),
    CorpusEntry("os_fdopen", "`os.fdopen(...)`", """
import os
fd = 3
os.fdopen(fd, "rb")
""", True),
    CorpusEntry("io_open", "`io.open(path)`", """
import io
path = "/tmp/x"
io.open(path)
""", True),
    CorpusEntry("aliased_builtin", 'aliased builtin (`helper = open; helper(path)`)', """
path = "/tmp/x"
helper = open
helper(path)
""", True),
    CorpusEntry("getattr_dispatch", '`getattr(path, "open")()`', """
from pathlib import Path
path = Path("/tmp/x")
getattr(path, "open")()
""", True),
    CorpusEntry("wrapper_function", "a wrapper function taking a path", """
def read_it(path):
    return open(path).read()

read_it("/tmp/x")
""", True),
    CorpusEntry("lambda_wrapper", "a lambda wrapping open", """
reader = lambda path: open(path).read()
reader("/tmp/x")
""", True),
    CorpusEntry("contextmanager_wrapper", "a contextmanager wrapping open", """
import contextlib

@contextlib.contextmanager
def opened(path):
    fh = open(path)
    try:
        yield fh
    finally:
        fh.close()
""", True),
    CorpusEntry("import_alias_path", 'import alias (`from pathlib import Path as P`)', """
from pathlib import Path as P
path = "/tmp/x"
P(path).open()
""", True),
    CorpusEntry("subclassed_pathlike", "a subclassed Path-like object", """
from pathlib import Path

class EvidencePath(Path):
    pass

p = EvidencePath("/tmp/x")
p.open()
""", True),
    CorpusEntry("os_listdir", "`os.listdir`", """
import os
os.listdir("/tmp")
""", True),
    CorpusEntry("os_walk", "`os.walk`", """
import os
list(os.walk("/tmp"))
""", True),
    CorpusEntry("os_path_realpath", "`os.path.realpath`", """
import os
os.path.realpath("/tmp/x")
""", True),
    CorpusEntry("path_stat", "`Path.stat`", """
from pathlib import Path
Path("/tmp/x").stat()
""", True),
    CorpusEntry("path_lstat", "`Path.lstat`", """
from pathlib import Path
Path("/tmp/x").lstat()
""", True),
    CorpusEntry("shutil_copyfile", "`shutil.copyfile`", """
import shutil
shutil.copyfile("/tmp/a", "/tmp/b")
""", True),
)

SAFE: tuple[CorpusEntry, ...] = (
    CorpusEntry("evidence_fs_presence", "known-good: evidence_fs.presence", """
from app.realtime import evidence_fs
path = "/tmp/x"
present, why = evidence_fs.presence(path)
""", False),
    CorpusEntry("evidence_fs_bounded_read", "known-good: evidence_fs.bounded_read", """
from app.realtime import evidence_fs
path = "/tmp/x"
data, why = evidence_fs.bounded_read(path)
""", False),
    CorpusEntry("evidence_fs_open_bounded_gzip", "known-good: evidence_fs.open_bounded_gzip", """
from app.realtime import evidence_fs
path = "/tmp/x"
with evidence_fs.open_bounded_gzip(path) as fh:
    fh.read()
""", False),
    CorpusEntry("evidence_fs_safe_enumerate", "known-good: evidence_fs.safe_enumerate", """
from app.realtime import evidence_fs
directory = "/tmp"
children, err = evidence_fs.safe_enumerate(directory, "segment=*")
""", False),
    CorpusEntry("evidence_fs_containment_reason", "known-good: evidence_fs.containment_reason", """
from app.realtime import evidence_fs
root, target = "/tmp", "/tmp/x"
reason = evidence_fs.containment_reason(root, target)
""", False),
    CorpusEntry("evidence_fs_is_regular_file", "known-good: evidence_fs.is_regular_file", """
from app.realtime import evidence_fs
path = "/tmp/x"
ok = evidence_fs.is_regular_file(path)
""", False),
    CorpusEntry("plain_dict_get", "negative control: dict.get must not false-positive as a queue primitive", """
d = {"a": 1}
d.get("a")
""", False),
    CorpusEntry("plain_str_join", "negative control: str.join must not false-positive", """
"-".join(["a", "b"])
""", False),
)

ALL: tuple[CorpusEntry, ...] = UNSAFE + SAFE
