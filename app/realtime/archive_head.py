"""KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 — genesis, head generations, current head.

This module replaces the append-only head log, and it exists because of one
inference that could not be made safe:

    the head is missing, therefore this must be a new archive

Everything built on that inference was defeated. The guard that tried to rescue
it ran only on the first commit, so an attacker suppressed the condition for a
single moment — by renaming peer manifests aside around each commit, or by
building the history in another root and moving it over — and then rebuilt an
arbitrary history through the exported commit function, with a segment deleted
or fabricated, and the verifier agreed. No digest had to be recomputed.

So an archive is now something that must be **explicitly created**, once, and
the collector can only ever *consume* one:

    archive-genesis.json      minted by an explicit init, never by the collector
    heads/<generation>.json   immutable, create-once, one per generation
    archive-head.json         the current pointer, and ONLY the current pointer
    archive.lock              persistent flock; never unlinked

Genesis missing is not "a new archive". It is a HALT.

Two properties do the work the log could not:

**Head generations are immutable and create-once.** The log was a single mutable
file, so truncating its tail rolled history back, and a torn final line poisoned
every future append. A generation record is published with `os.link`, which
fails with EEXIST if that generation already exists — a generation can never be
rewritten, only created, and there is exactly one artifact per generation to
find missing.

**`archive-head.json` is a pointer, not the history.** It carries the generation
and that generation record's digest and nothing else, so a head that disagrees
with the newest generation record on disk is a detectable state (STALE_HEAD)
rather than a silent rewrite. The previous design put the head's own
non-deterministic `updated_at` into the log so a crashed commit could be
replayed byte-exactly — which handed an attacker the last input needed to
reconstruct any earlier head and made byte-exact rollback free. Nothing here
publishes an input for that purpose: a crashed commit is finished by finding the
generation record that was already durably created.

## Threat model, stated exactly

Corrected for the third time, because this section has overclaimed in every
previous revision and a reviewer has caught it every time. What follows is what
the code demonstrably does, not what the design aspires to.

**Pinned without any external input.** A history that is SHORTER than it claims.
Generation N asserts `segment_count == N`, so dropping a segment drops a
generation, and a no-op generation minted to restore the counter contradicts its
own segment count. Also: any edit to a single artifact, since manifests,
generation records and the pointer must agree with each other.

**Pinned only with the external constant `expected_archive_id`.** An archive
minted elsewhere and moved into place. Without that constant the genesis file is
a LABEL, not an anchor — `genesis_digest` is unkeyed and recomputable, so an
attacker forges a genesis as easily as reading one.

**Pinned only with the external anchor `expected_head=(generation, digest)`.**
The committed ORDER and the committed SET of segments. An attacker who rewrites
every generation record and the pointer — leaving the genesis and every manifest
byte-identical, and needing to forge nothing — can reorder committed history, or
substitute and insert segments while preserving the generation count. This is
NOT covered by "an attacker who rewrites the root of trust", which earlier
revisions of this comment claimed as the only residual. It is a full-chain
re-mint, and it is defeated only by an anchor held outside this root. Because
generations chain, pinning one generation pins every generation before it.

`minimum_generation` pins a COUNTER and nothing else. It stops shrinkage; it does
not stop reordering, substitution or insertion, all of which can grow past it.
Prefer `expected_head`.

**Not pinned at all.** `archive_identity`, `created_at`/`updated_at` and
`canonical_schema_version` in every artifact, and `environment` in the pointer:
self-digest only, freely relabelled. Nothing reads them for verification. So the
"any edit to a single artifact" claim above holds for every field EXCEPT these —
an earlier revision of this paragraph asserted it without the exception and was
wrong about `updated_at`.

No arrangement of files under a root the writer can write can close the
full-chain re-mint. That requires either a key the writer does not hold or an
append-only sink it cannot rewrite, and this module implements neither.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.realtime.canonical import (
    CANONICAL_SCHEMA_VERSION,
    canonical_bytes,
    canonical_datetime,
    digest_hex,
    parse_canonical,
)

GENESIS_FILENAME = "archive-genesis.json"
CURRENT_HEAD_FILENAME = "archive-head.json"
HEADS_DIRNAME = "heads"
ARCHIVE_LOCK_FILENAME = "archive.lock"
TEMP_SUFFIX = ".tmp"

GENESIS_SCHEMA_VERSION = 1
HEAD_SCHEMA_VERSION = 2          # bumped: the log-era head is not readable here
CURRENT_HEAD_SCHEMA_VERSION = 1

# Every field carries a stated purpose. Nothing is here because later code would
# find it convenient.
GENESIS_FIELDS = (
    "schema_version",            # envelope version for this artifact
    "canonical_schema_version",  # digests only compare within one encoding
    "archive_id",                # THE root of trust: minted once, never derived
    "environment",               # demo evidence must never become production
    "archive_identity",          # the logical archive this root serves
    "created_at",                # when the archive was brought into existence
)

# A generation record is a DELTA: exactly one transition from N-1.
#
# It used to embed the whole ordered segment list, and that was the defect. The
# verifier checked only `archive_id` and `previous_head_digest` between
# generations, so nothing tied the generation NUMBER to the segment COUNT — an
# attacker deleted a middle segment, re-minted generations 1..N with the
# shortened list, repointed, and the archive verified VALID with the genesis
# byte-identical and both `expected_archive_id` and `minimum_generation`
# satisfied. Twelve records became eight.
#
# As a delta the arithmetic does the work instead of a comparison anyone can
# forget to write: generation N asserts `segment_count == N`, so a history that
# is one segment shorter is one GENERATION shorter, and a no-op generation
# minted to restore the counter contradicts its own segment count. It also
# makes the records O(1) rather than O(N) — the embedded list reached 75 KiB per
# record and 9.5 MiB of heads at 250 segments, which for an hourly cadence is
# ~11.8 GB/year of head records alone.
HEAD_GENERATION_FIELDS = (
    "schema_version",
    "canonical_schema_version",
    "archive_id",                 # binds the generation to THIS archive's genesis
    "environment",                # demo evidence must never become production
    "generation",                 # position in the chain; the file name mirrors it
    "previous_head_digest",       # chain link to generation N-1 (None at 0)
    "segment_count",              # cumulative; MUST equal `generation`
    "committed_segment_id",       # the one segment this transition adds (None at 0)
    "committed_segment_digest",   # its commitment
    "committed_record_count",     # so the head's claim is checkable without the manifest
    "committed_partition_identity",
    "previous_segment_digest",    # the predecessor at COMMIT time — the chain order
    "first_segment_digest",       # position 0, carried forward unchanged
    "archive_segments_digest",    # cumulative position-bound fold
    "created_at",
)

CURRENT_HEAD_FIELDS = (
    "schema_version",
    "canonical_schema_version",
    "archive_id",
    "environment",
    "generation",                # which generation record is authoritative now
    "head_digest",               # that record's digest, so the pair cannot drift
    "updated_at",
)

_GENERATION_FILE_RE = re.compile(r"^(\d{16})\.json$")


class ArchiveHeadError(RuntimeError):
    """A head operation that would compromise the archive's history."""


class ArchiveNotInitializedError(ArchiveHeadError):
    """No genesis marker. This is a HALT, never an invitation to bootstrap."""


class ArchiveIdentityMismatch(ArchiveHeadError):
    """The archive on disk is not the archive this process was configured for."""


class HeadRecoveryRequired(ArchiveHeadError):
    """The archive is intact but a head transition did not finish."""


class DurabilityNotProven(ArchiveHeadError):
    """Renamed into place, but the fsync that would prove it survives failed."""


# --- paths ------------------------------------------------------------------------


def env_root(root, environment: str) -> Path:
    return Path(root) / f"env={environment}"


def genesis_path(root, environment: str) -> Path:
    return env_root(root, environment) / GENESIS_FILENAME


def current_head_path(root, environment: str) -> Path:
    return env_root(root, environment) / CURRENT_HEAD_FILENAME


def heads_dir(root, environment: str) -> Path:
    return env_root(root, environment) / HEADS_DIRNAME


def generation_path(root, environment: str, generation: int) -> Path:
    return heads_dir(root, environment) / f"{generation:016d}.json"


def archive_lock_path(root, environment: str) -> Path:
    return env_root(root, environment) / ARCHIVE_LOCK_FILENAME


# --- the archive lock -------------------------------------------------------------


@contextlib.contextmanager
def archive_lock(root, environment: str, timeout_s: float = 30.0):
    """Serialize every head mutation across threads AND processes.

    The lock file is created once and **never unlinked**. Unlinking on release
    was a defect, not a tidy-up: a second `_release_lock()` on an
    already-released writer deleted whatever was at that path — including a live
    successor's lock file — after which the next process created a fresh inode,
    took an flock on it trivially, and two owners appended to one segment. Six
    records went in and none came out.

    Ownership is the flock, not the file. The kernel drops it when the holder
    dies, so a stale file is inert and a live holder is unambiguous. Releasing
    means closing the descriptor and nothing else.
    """
    path = archive_lock_path(root, environment)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600)
    deadline = time.monotonic() + timeout_s
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise ArchiveHeadError(
                        f"timed out after {timeout_s}s waiting for the archive "
                        f"head lock at {path}")
                time.sleep(0.01)
        yield
    finally:
        os.close(fd)              # releases the flock; the FILE stays


# --- durable publication ----------------------------------------------------------


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_all(fd: int, payload: bytes) -> int:
    from app.realtime.segment import write_all
    return write_all(fd, payload)


def _stage_bytes(directory: Path, name: str, payload: bytes, *, stage=None) -> Path:
    """Write a fully-formed temp artifact and prove it is the bytes we meant."""
    def _s(n):
        if stage is not None:
            stage(n)

    tmp = directory / f"{name}.{os.getpid()}.{os.getpid() ^ id(payload):x}{TEMP_SUFFIX}"
    _s(f"{name}_temp_create")
    try:
        if os.path.lexists(tmp):
            os.unlink(tmp)
    except OSError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                 | os.O_CLOEXEC, 0o600)
    try:
        _s(f"{name}_write")
        _write_all(fd, payload)
        _s(f"{name}_fsync")
        os.fsync(fd)
    finally:
        os.close(fd)
    _s(f"{name}_verify")
    staged = tmp.read_bytes()
    if staged != payload:
        raise ArchiveHeadError(
            f"staged {name} is {len(staged)} bytes, expected {len(payload)}; "
            "refusing to publish bytes that are not the ones we meant to commit")
    return tmp


def _publish_create_once(tmp: Path, final: Path, *, stage=None, label: str) -> None:
    """Publish so that the destination can be CREATED but never REPLACED.

    `os.link` fails with EEXIST if `final` exists, which is what makes a
    generation record immutable in the filesystem rather than by convention. A
    rename would silently overwrite, and overwriting a generation is the whole
    class of attack this module removes.
    """
    if stage is not None:
        stage(f"{label}_publish")
    if final.is_symlink():
        raise ArchiveHeadError(
            f"{final} is a symlink; refusing to publish history through it")
    try:
        os.link(tmp, final)
    except FileExistsError:
        os.unlink(tmp)
        raise ArchiveHeadError(
            f"{final.name} already exists; a head generation is created once "
            "and never rewritten") from None
    os.unlink(tmp)
    try:
        if stage is not None:
            stage(f"{label}_directory_fsync")
        _fsync_directory(final.parent)
    except OSError as exc:
        raise DurabilityNotProven(
            f"{final} was linked into place and IS visible to readers, but the "
            f"directory fsync that would prove it survives a crash failed: "
            f"{exc!r}. The record is committed; its durability is not proven.")


def _publish_replace(tmp: Path, final: Path, *, stage=None, label: str) -> None:
    """Publish a pointer by atomic rename. Only the current head uses this."""
    if stage is not None:
        stage(f"{label}_publish")
    if final.is_symlink():
        raise ArchiveHeadError(
            f"{final} is a symlink; refusing to publish the current head "
            "through it")
    os.replace(tmp, final)
    try:
        if stage is not None:
            stage(f"{label}_directory_fsync")
        _fsync_directory(final.parent)
    except OSError as exc:
        raise DurabilityNotProven(
            f"{final} was renamed into place and IS visible to readers, but the "
            f"directory fsync that would prove it survives a crash failed: "
            f"{exc!r}. The pointer is committed; its durability is not proven.")


def _read_json(path: Path, *, what: str) -> dict:
    if path.is_symlink():
        raise ArchiveHeadError(
            f"{path} is a symlink; the {what} must live inside the archive root")
    try:
        value = parse_canonical(path.read_bytes())
    except OSError as exc:
        raise ArchiveHeadError(f"{what} is unreadable: {exc!r}") from exc
    except Exception as exc:                      # noqa: BLE001 - reported
        raise ArchiveHeadError(
            f"{what} is not canonical JSON ({type(exc).__name__})") from exc
    if not isinstance(value, dict):
        raise ArchiveHeadError(
            f"{what} is a {type(value).__name__}, not an object")
    return value


# --- digests ----------------------------------------------------------------------


def genesis_digest_of(genesis: dict) -> str:
    return digest_hex({k: genesis.get(k) for k in GENESIS_FIELDS})


def head_digest_of(head: dict) -> str:
    return digest_hex({k: head.get(k) for k in HEAD_GENERATION_FIELDS})


def current_head_digest_of(pointer: dict) -> str:
    return digest_hex({k: pointer.get(k) for k in CURRENT_HEAD_FIELDS})


def segment_commitment(manifest: dict) -> str:
    """The single value that stands for one committed segment."""
    return manifest["manifest_digest"]


def fold_segments_digest(previous: str, commitment: str) -> str:
    """Position-bound fold: reordering two segments changes the result."""
    return digest_hex({"previous": previous, "segment": commitment})


def _segments_genesis(archive_id: str, environment: str) -> str:
    return digest_hex({"archive_id": archive_id, "environment": environment,
                       "purpose": "archive-segments-fold"})


# --- genesis ----------------------------------------------------------------------


def initialize_archive(root, environment: str, *, archive_identity: str,
                       archive_id: str | None = None) -> dict:
    """Bring an archive into existence. The ONLY way a genesis is minted.

    Explicitly separate from the collector, and refuses to run twice. The
    collector calls nothing in this function's path — that separation is the
    integrity boundary, and making the commit helpers private is only
    defence in depth on top of it.
    """
    directory = env_root(root, environment)
    directory.mkdir(parents=True, exist_ok=True)
    from app.realtime.segment import assert_contained
    assert_contained(root, directory)
    path = genesis_path(root, environment)
    if os.path.lexists(path):
        raise ArchiveHeadError(
            f"{path} already exists; this archive has already been initialized "
            "and re-initializing it would mint a second root of trust for the "
            "same evidence")
    heads = heads_dir(root, environment)
    heads.mkdir(parents=True, exist_ok=True)
    assert_contained(root, heads)

    requested_id = archive_id

    # ORDER MATTERS, and the previous order was a permanent brick. Linking the
    # genesis FIRST meant a crash before generation 0 left an archive that could
    # not move: `head_state` said RECOVERY_REQUIRED, recovery refused ("no head
    # generation records at all"), re-initialization refused ("already
    # initialized"), and commit refused. Worse, `EventArchive.__init__` checks
    # only the genesis, so every collector restart accepted events, published a
    # manifest, and only then discovered the head was unusable — burning another
    # unresolvable orphan each time.
    #
    # Publishing generation 0 and the pointer first inverts it: a crash before
    # the genesis link leaves NOT_INITIALIZED, which initialization may
    # legitimately re-run. "Genesis exists" now IMPLIES "generation 0 and the
    # pointer exist".
    existing_zero = generation_path(root, environment, 0)
    if os.path.lexists(existing_zero):
        # A previous interrupted initialization already created it. Adopt the
        # record AND ITS IDENTITY. The first version of this adopted the record
        # while the genesis kept a freshly minted uuid4, so the two essentially
        # never matched and two of the three crash windows produced a
        # permanently HEAD_INVALID archive with every remedy closed — worse than
        # the brick it replaced, and plantable by dropping a foreign empty
        # generation 0 into a virgin root.
        durable = read_generation(root, environment, 0)
        if durable.get("segment_count") != 0 or durable.get("committed_segment_id"):
            raise ArchiveHeadError(
                f"{existing_zero} exists and is not an empty generation 0; "
                "refusing to initialize over it")
        if durable.get("environment") != environment:
            raise ArchiveIdentityMismatch(
                f"{existing_zero} belongs to environment "
                f"{durable.get('environment')!r}, not {environment!r}")
        # Compare against what the CALLER asked for, which may be None. The
        # previous version minted a fresh uuid4 thirty-six lines earlier and
        # then compared the durable record against that random value, so the
        # match was impossible and `archive_id = durable[...]` was unreachable
        # for every default caller. Third occurrence of this brick, and it was
        # always a one-line ordering error.
        if requested_id is not None and durable.get("archive_id") != requested_id:
            raise ArchiveIdentityMismatch(
                f"{existing_zero} belongs to archive "
                f"{durable.get('archive_id')!r}, but initialization was asked "
                f"for {requested_id!r}")
        archive_id = durable["archive_id"]
        zero = durable
    else:
        archive_id = requested_id or uuid.uuid4().hex
        zero = _build_generation_zero(archive_id=archive_id,
                                      environment=environment)
        _publish_generation(root, environment, zero)

    # The genesis is built AFTER the identity is settled, so "genesis exists"
    # implies "generation 0 exists AND agrees with it".
    genesis = {
        "schema_version": GENESIS_SCHEMA_VERSION,
        "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
        "archive_id": archive_id,
        "environment": environment,
        "archive_identity": archive_identity,
        "created_at": canonical_datetime(datetime.now(timezone.utc)),
    }
    genesis["genesis_digest"] = genesis_digest_of(genesis)
    _publish_current_head(root, environment, zero)

    payload = canonical_bytes(genesis)
    tmp = _stage_bytes(directory, GENESIS_FILENAME, payload)
    _publish_create_once(tmp, path, label="genesis")
    return genesis


def read_genesis(root, environment: str, *,
                 expected_archive_id: str | None = None) -> dict:
    """Load the root of trust, or refuse to proceed.

    There is deliberately no `None` return and no "create if absent" path. A
    caller that cannot find the genesis has not found a new archive; it has
    found an archive whose root of trust is gone.
    """
    path = genesis_path(root, environment)
    if not os.path.lexists(path):
        raise ArchiveNotInitializedError(
            f"no genesis marker at {path}. This archive was never initialized, "
            "or its root of trust has been removed. A missing genesis is NEVER "
            "treated as a new archive: bootstrapping one here is exactly how a "
            "rebuilt history certifies the deletion it should detect. Run the "
            "explicit archive initialization for a genuinely new root, or "
            "restore the genesis from backup.")
    genesis = _read_json(path, what="archive genesis")
    unknown = sorted(set(genesis) - set(GENESIS_FIELDS) - {"genesis_digest"})
    if unknown:
        raise ArchiveHeadError(
            f"genesis carries undeclared top-level field(s) {unknown}; the "
            "envelope is closed at this schema version, so an extra key would "
            "sit outside every digest and every commitment")
    if genesis.get("genesis_digest") != genesis_digest_of(genesis):
        raise ArchiveHeadError(
            "the archive genesis fails its own digest; the root of trust is "
            "not intact")
    if genesis.get("schema_version") != GENESIS_SCHEMA_VERSION:
        raise ArchiveHeadError(
            f"genesis schema_version {genesis.get('schema_version')!r} is not "
            f"{GENESIS_SCHEMA_VERSION}")
    if genesis.get("environment") != environment:
        raise ArchiveIdentityMismatch(
            f"genesis environment {genesis.get('environment')!r} != "
            f"{environment!r}")
    if expected_archive_id is not None and genesis.get("archive_id") != expected_archive_id:
        raise ArchiveIdentityMismatch(
            f"this root holds archive {genesis.get('archive_id')!r}, but this "
            f"process is configured for {expected_archive_id!r}. An archive "
            "built elsewhere and moved into place presents exactly this way.")
    return genesis


# --- head generations -------------------------------------------------------------


def _build_generation_zero(*, archive_id: str, environment: str) -> dict:
    head = {
        "schema_version": HEAD_SCHEMA_VERSION,
        "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
        "archive_id": archive_id,
        "environment": environment,
        "generation": 0,
        "previous_head_digest": None,
        "segment_count": 0,
        "committed_segment_id": None,
        "committed_segment_digest": None,
        "committed_record_count": None,
        "committed_partition_identity": None,
        "previous_segment_digest": None,
        "first_segment_digest": None,
        "archive_segments_digest": _segments_genesis(archive_id, environment),
        "created_at": canonical_datetime(datetime.now(timezone.utc)),
    }
    head["head_digest"] = head_digest_of(head)
    return head


def _build_generation(*, previous: dict, manifest: dict) -> dict:
    """The next generation as ONE transition from `previous`. Nothing else."""
    commitment = segment_commitment(manifest)
    head = {
        "schema_version": HEAD_SCHEMA_VERSION,
        "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
        "archive_id": previous["archive_id"],
        "environment": previous["environment"],
        "generation": previous["generation"] + 1,
        "previous_head_digest": previous["head_digest"],
        "segment_count": previous["segment_count"] + 1,
        "committed_segment_id": manifest["segment_id"],
        "committed_segment_digest": commitment,
        "committed_record_count": manifest["record_count"],
        "committed_partition_identity": manifest["partition_identity"],
        # Resolved HERE, under the archive lock, where the order is decided. A
        # writer cannot know its place in history when it opens: writers are
        # lazy and overlap, so two hours both opened against an empty head and
        # both recorded predecessor None, and every ordinary rollover produced a
        # permanently INVALID archive.
        "previous_segment_digest": previous["committed_segment_digest"],
        "first_segment_digest": (previous["first_segment_digest"]
                                 if previous["generation"] else commitment),
        "archive_segments_digest": fold_segments_digest(
            previous["archive_segments_digest"], commitment),
        "created_at": canonical_datetime(datetime.now(timezone.utc)),
    }
    head["head_digest"] = head_digest_of(head)
    return head


def verify_transition(previous: dict, current: dict) -> list:
    """Is `current` exactly one valid transition from `previous`?

    This is the check whose absence let a reduced history be re-minted into an
    apparently valid one. Every clause below is an equality the writer actually
    maintains, so a history that skipped, dropped or fabricated a step
    contradicts one of them.
    """
    out = []
    gen = current.get("generation")
    if gen != previous.get("generation", -1) + 1:
        out.append(f"generation {gen} does not follow {previous.get('generation')}")
    if current.get("previous_head_digest") != previous.get("head_digest"):
        out.append(f"generation {gen} does not chain from its predecessor")
    if current.get("segment_count") != (previous.get("segment_count") or 0) + 1:
        out.append(
            f"generation {gen} claims {current.get('segment_count')} segments; "
            f"exactly one segment is added per generation, so it must be "
            f"{(previous.get('segment_count') or 0) + 1}")
    if current.get("segment_count") != gen:
        out.append(
            f"generation {gen} claims {current.get('segment_count')} segments; "
            "generation N is reached by N transitions and must hold N segments "
            "(a re-minted shorter history presents exactly this way)")
    if current.get("previous_segment_digest") != previous.get("committed_segment_digest"):
        out.append(f"generation {gen} records the wrong predecessor segment")
    expected_fold = fold_segments_digest(
        previous.get("archive_segments_digest"),
        current.get("committed_segment_digest"))
    if current.get("archive_segments_digest") != expected_fold:
        out.append(
            f"generation {gen} archive_segments_digest does not extend its "
            "predecessor (deletion, insertion or reorder presents this way)")
    expected_first = (previous.get("first_segment_digest")
                      if previous.get("generation") else
                      current.get("committed_segment_digest"))
    if current.get("first_segment_digest") != expected_first:
        out.append(f"generation {gen} first_segment_digest was rewritten")
    for field in ("archive_id", "environment"):
        if current.get(field) != previous.get(field):
            out.append(f"generation {gen} {field} differs from its predecessor")
    return out


def _publish_generation(root, environment: str, head: dict, *, stage=None) -> Path:
    directory = heads_dir(root, environment)
    directory.mkdir(parents=True, exist_ok=True)
    from app.realtime.segment import assert_contained
    assert_contained(root, directory)
    final = generation_path(root, environment, head["generation"])
    payload = canonical_bytes(head)
    tmp = _stage_bytes(directory, f"{head['generation']:016d}.json", payload,
                       stage=stage)
    staged = parse_canonical(tmp.read_bytes())
    if staged.get("head_digest") != head_digest_of(staged):
        os.unlink(tmp)
        raise ArchiveHeadError(
            "the staged head generation does not verify against its own digest")
    _publish_create_once(tmp, final, stage=stage, label="head_generation")
    return final


def read_generation(root, environment: str, generation: int) -> dict:
    path = generation_path(root, environment, generation)
    if not os.path.lexists(path):
        raise ArchiveHeadError(f"head generation {generation} is missing")
    head = _read_json(path, what=f"head generation {generation}")
    unknown = sorted(set(head) - set(HEAD_GENERATION_FIELDS) - {"head_digest"})
    if unknown:
        raise ArchiveHeadError(
            f"head generation {generation} carries undeclared top-level "
            f"field(s) {unknown}; the envelope is closed at this schema version")
    if head.get("schema_version") != HEAD_SCHEMA_VERSION:
        raise ArchiveHeadError(
            f"head generation {generation} schema_version "
            f"{head.get('schema_version')!r} is not {HEAD_SCHEMA_VERSION}")
    if not isinstance(head.get("generation"), int) or isinstance(head.get("generation"), bool):
        raise ArchiveHeadError(
            f"head generation {generation} declares a non-integer generation "
            f"{head.get('generation')!r}")
    if head.get("head_digest") != head_digest_of(head):
        raise ArchiveHeadError(
            f"head generation {generation} fails its own digest")
    if head.get("generation") != generation:
        raise ArchiveHeadError(
            f"{path.name} declares generation {head.get('generation')!r}; a "
            "generation record renamed over another presents this way")
    return head


def present_generations(root, environment: str) -> list:
    directory = heads_dir(root, environment)
    if not directory.is_dir() or directory.is_symlink():
        return []
    out = []
    for child in directory.iterdir():
        m = _GENERATION_FILE_RE.match(child.name)
        if m and not child.is_symlink() and child.is_file():
            out.append(int(m.group(1)))
    return sorted(out)


# --- current head -----------------------------------------------------------------


def _publish_current_head(root, environment: str, head: dict, *, stage=None) -> Path:
    directory = env_root(root, environment)
    pointer = {
        "schema_version": CURRENT_HEAD_SCHEMA_VERSION,
        "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
        "archive_id": head["archive_id"],
        "environment": head["environment"],
        "generation": head["generation"],
        "head_digest": head["head_digest"],
        "updated_at": canonical_datetime(datetime.now(timezone.utc)),
    }
    pointer["current_head_digest"] = current_head_digest_of(pointer)
    payload = canonical_bytes(pointer)
    tmp = _stage_bytes(directory, CURRENT_HEAD_FILENAME, payload, stage=stage)
    _publish_replace(tmp, current_head_path(root, environment), stage=stage,
                     label="current_head")
    return current_head_path(root, environment)


def read_current_head(root, environment: str) -> dict:
    path = current_head_path(root, environment)
    if not os.path.lexists(path):
        raise HeadRecoveryRequired(
            f"the archive genesis exists but {path} does not. The archive is "
            "initialized, so this is NOT a new archive and must not be "
            "bootstrapped. Either a head transition was interrupted (run "
            "recovery) or the current head was removed (restore it).")
    pointer = _read_json(path, what="current archive head")
    unknown = sorted(set(pointer) - set(CURRENT_HEAD_FIELDS) - {"current_head_digest"})
    if unknown:
        raise ArchiveHeadError(
            f"the current head carries undeclared top-level field(s) {unknown}; "
            "the envelope is closed at this schema version")
    gen = pointer.get("generation")
    if not isinstance(gen, int) or isinstance(gen, bool) or gen < 0:
        # `generation_path` formats this with %016d, so a str/list/None escaped
        # as a raw ValueError/TypeError out of every monitor and every reader.
        raise ArchiveHeadError(
            f"the current head declares a non-integer generation {gen!r}")
    if pointer.get("current_head_digest") != current_head_digest_of(pointer):
        raise ArchiveHeadError("the current archive head fails its own digest")
    if pointer.get("schema_version") != CURRENT_HEAD_SCHEMA_VERSION:
        raise ArchiveHeadError(
            f"current head schema_version {pointer.get('schema_version')!r} is "
            f"not {CURRENT_HEAD_SCHEMA_VERSION}")
    return pointer


@dataclass
class AuthoritativeHead:
    genesis: dict
    pointer: dict
    generation_record: dict

    @property
    def generation(self) -> int:
        return self.generation_record["generation"]


def load_authoritative_head(root, environment: str, *,
                            expected_archive_id: str | None = None
                            ) -> AuthoritativeHead:
    """Genesis → pointer → generation record, each checked against the last."""
    genesis = read_genesis(root, environment,
                           expected_archive_id=expected_archive_id)
    pointer = read_current_head(root, environment)
    if pointer.get("archive_id") != genesis["archive_id"]:
        raise ArchiveIdentityMismatch(
            "the current head belongs to a different archive than the genesis")
    record = read_generation(root, environment, pointer["generation"])
    if record["head_digest"] != pointer.get("head_digest"):
        raise ArchiveHeadError(
            f"the current head points at generation {pointer['generation']} "
            "with a digest that generation's record does not have; the pointer "
            "and the history disagree")
    if record["archive_id"] != genesis["archive_id"]:
        raise ArchiveIdentityMismatch(
            "the head generation belongs to a different archive than the genesis")
    return AuthoritativeHead(genesis, pointer, record)


# --- commit -----------------------------------------------------------------------


def _commit_locked(root, environment: str, *, manifest: dict,
                   expected_archive_id: str | None, stage=None) -> dict:
    genesis = read_genesis(root, environment,
                           expected_archive_id=expected_archive_id)
    current = load_authoritative_head(
        root, environment, expected_archive_id=expected_archive_id)
    previous = current.generation_record
    generation = previous["generation"] + 1

    if manifest.get("environment") != environment:
        raise ArchiveHeadError(
            f"refusing to commit a {manifest.get('environment')!r} segment into "
            f"the {environment!r} archive")
    if _segment_already_committed(root, environment, previous,
                                  manifest["segment_id"]):
        raise ArchiveHeadError(
            f"segment {manifest['segment_id']!r} is already in the archive head")

    head = _build_generation(previous=previous, manifest=manifest)

    existing = generation_path(root, environment, generation)
    if os.path.lexists(existing):
        # A previous attempt durably created this generation and died before the
        # pointer moved. Finish THAT transition rather than minting a second
        # one: the record on disk is the commitment, and it is immutable.
        durable = read_generation(root, environment, generation)
        if durable.get("committed_segment_digest") != head["committed_segment_digest"]:
            raise ArchiveHeadError(
                f"head generation {generation} already exists and commits "
                f"segment {durable.get('committed_segment_id')!r}, not "
                f"{manifest['segment_id']!r}. Complete or resolve that "
                "transition before committing this segment.")
        head = durable
    else:
        _publish_generation(root, environment, head, stage=stage)

    _publish_current_head(root, environment, head, stage=stage)
    return head


def _segment_already_committed(root, environment: str, head: dict,
                               segment_id: str) -> bool:
    """Walk the delta chain backwards looking for this segment id.

    O(N) reads rather than an O(1) list membership test, which is the cost of
    dropping the embedded segment list. It runs once per commit, under the
    archive lock, against records that are a few hundred bytes each.
    """
    generation = head.get("generation") or 0
    while generation > 0:
        rec = read_generation(root, environment, generation)
        if rec.get("committed_segment_id") == segment_id:
            return True
        generation -= 1
    return False


def commit_segment(root, environment: str, *, manifest: dict,
                   expected_archive_id: str | None = None, stage=None) -> dict:
    """Append one committed segment to the archive's authoritative history."""
    with archive_lock(root, environment):
        return _commit_locked(root, environment, manifest=manifest,
                              expected_archive_id=expected_archive_id,
                              stage=stage)


def recover_current_head(root, environment: str, *,
                         expected_archive_id: str | None = None) -> dict:
    """Finish an already-committed generation transition. Never rebuild one.

    This advances the pointer to generation N only when N's record is durably
    on disk, chains from the current generation, and is the only candidate.
    That is finishing a transition, not discovering a history: nothing here
    reads segment directories to decide what the history should have been.
    """
    with archive_lock(root, environment):
        genesis = read_genesis(root, environment,
                               expected_archive_id=expected_archive_id)
        try:
            pointer = read_current_head(root, environment)
        except HeadRecoveryRequired:
            # Case D. The newest generation record is the only statement of
            # what the head was, and it is immutable, so re-pointing at it is
            # not discovery — but it IS a repair, so it is explicit and here.
            present = present_generations(root, environment)
            if not present:
                raise ArchiveHeadError(
                    "the archive is initialized but has no head generation "
                    "records at all; its history is gone and cannot be "
                    "reconstructed from the segments on disk") from None
            record = read_generation(root, environment, present[-1])
            # The SAME checks the pointer-intact branch applies. Deleting one
            # file must not downgrade recovery from verified to trusting: a
            # planted generation with a foreign archive_id, a broken chain or a
            # phantom segment was adopted and WRITTEN, converting a recoverable
            # RECOVERY_REQUIRED into an unrecoverable HEAD_INVALID.
            if record.get("archive_id") != genesis["archive_id"]:
                raise ArchiveIdentityMismatch(
                    f"head generation {present[-1]} belongs to a different "
                    "archive than the genesis; refusing to adopt it")
            if present != list(range(present[-1] + 1)):
                raise ArchiveHeadError(
                    f"head generations {present} are not contiguous from 0; "
                    "refusing to adopt the newest as the current head")
            if present[-1] > 0:
                prior = read_generation(root, environment, present[-1] - 1)
                problems = verify_transition(prior, record)
                if problems:
                    raise ArchiveHeadError(
                        f"head generation {present[-1]} is not a valid "
                        f"transition: {problems}")
                seg_id = record.get("committed_segment_id")
                seg_dir = env_root(root, environment) / f"segment={seg_id}"
                from app.realtime.segment import MANIFEST_FILENAME, verify_segment
                if not _manifest_present(seg_dir):
                    raise ArchiveHeadError(
                        f"head generation {present[-1]} commits segment "
                        f"{seg_id!r}, whose manifest is not on disk or is "
                        "unreadable")
                verdict = verify_segment(seg_dir, environment=environment,
                                         root=root)
                if not verdict.valid:
                    raise ArchiveHeadError(
                        f"head generation {present[-1]} commits segment "
                        f"{seg_id!r}, which does not verify: {verdict.reasons}")
            _publish_current_head(root, environment, record)
            return record
        current = read_generation(root, environment, pointer["generation"])
        if current["head_digest"] != pointer.get("head_digest"):
            raise ArchiveHeadError(
                "the current head and its generation record disagree; this is "
                "not a recoverable interrupted transition")
        nxt = pointer["generation"] + 1
        if not os.path.lexists(generation_path(root, environment, nxt)):
            return current                     # nothing in flight
        candidate = read_generation(root, environment, nxt)
        if candidate.get("previous_head_digest") != current["head_digest"]:
            raise ArchiveHeadError(
                f"head generation {nxt} does not chain from the current head; "
                "refusing to advance")
        if candidate.get("archive_id") != genesis["archive_id"]:
            raise ArchiveIdentityMismatch(
                f"head generation {nxt} belongs to a different archive")
        if os.path.lexists(generation_path(root, environment, nxt + 1)):
            raise ArchiveHeadError(
                f"head generations {nxt} and {nxt + 1} both exist while the "
                f"current head is at {pointer['generation']}; more than one "
                "transition is outstanding and this needs an operator")
        seg_id = candidate.get("committed_segment_id")
        seg_dir = env_root(root, environment) / f"segment={seg_id}"
        from app.realtime.segment import MANIFEST_FILENAME, verify_segment
        if not _manifest_present(seg_dir):
            raise ArchiveHeadError(
                f"head generation {nxt} commits segment {seg_id!r}, whose "
                "manifest is not on disk or is unreadable; refusing to advance "
                "the head to a generation whose evidence is absent")
        verdict = verify_segment(seg_dir, environment=environment, root=root)
        if not verdict.valid:
            raise ArchiveHeadError(
                f"head generation {nxt} commits segment {seg_id!r}, which does "
                f"not verify: {verdict.reasons}")
        _publish_current_head(root, environment, candidate)
        return candidate


def _manifest_present(seg_dir) -> bool:
    """`Path.exists()` raises EACCES; the repair path must not die on it."""
    from app.realtime.segment import MANIFEST_FILENAME
    try:
        return os.path.lexists(Path(seg_dir) / MANIFEST_FILENAME)
    except OSError:
        return False


def head_state(root, environment: str, *,
               expected_archive_id: str | None = None) -> dict:
    """Classify the head without raising. For verification and operators."""
    try:
        genesis = read_genesis(root, environment,
                               expected_archive_id=expected_archive_id)
    except ArchiveNotInitializedError as exc:
        return {"state": "NOT_INITIALIZED", "reason": str(exc)}
    except ArchiveHeadError as exc:
        return {"state": "GENESIS_INVALID", "reason": str(exc)}
    try:
        present = present_generations(root, environment)
    except OSError as exc:
        return {"state": "HEAD_INVALID",
                "reason": f"the heads directory is unreadable: {exc!r}",
                "archive_id": genesis["archive_id"], "generations": []}
    try:
        head = load_authoritative_head(root, environment,
                                       expected_archive_id=expected_archive_id)
    except HeadRecoveryRequired as exc:
        return {"state": "RECOVERY_REQUIRED", "reason": str(exc),
                "archive_id": genesis["archive_id"], "generations": present}
    except ArchiveHeadError as exc:
        return {"state": "HEAD_INVALID", "reason": str(exc),
                "archive_id": genesis["archive_id"], "generations": present}
    except Exception as exc:                  # noqa: BLE001 - a verdict, not a crash
        return {"state": "HEAD_INVALID",
                "reason": f"{type(exc).__name__}: {exc}",
                "archive_id": genesis["archive_id"], "generations": present}
    newest = present[-1] if present else None
    if newest is not None and newest > head.generation:
        return {"state": "STALE_HEAD", "head": head, "generations": present,
                "archive_id": genesis["archive_id"],
                "reason": f"generation {newest} is durably committed but the "
                          f"current head still points at {head.generation}; a "
                          "transition was interrupted and can be completed"}
    return {"state": "CURRENT", "head": head, "generations": present,
            "archive_id": genesis["archive_id"]}
