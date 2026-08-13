# KALSHI-ARCHIVE-VERIFICATION-META-001

Two prior agents built the meta-verification layer against `app/realtime/`
(A1-A8, report-only, `app/` never touched). This round did two things:

1. **Consolidated the two AST guards into one** (blocking prerequisite).
2. **A9 -- the mutation campaign**: a committed, re-runnable proof that the
   meta-verification layer itself fails when it should.

`app/` is untouched by any commit in this milestone. Everything below lives
under `tests/`.

## 1. Guard consolidation

`tests/harness_filesystem_totality/ast_audit.py` (the original guard) and
`tests/meta_inventory/ast_guard_v2.py` (a strengthened guard built to close
the original's blind spots) covered the same four production modules with
different vocabularies. A mutation campaign against only one of them would
have certified nothing -- the other guard would still pass. `ast_guard_v2.py`
is deleted; its detections (wider attribute/module vocabulary, execution-
order alias tracking for builtin rebinding and import aliasing, `getattr(...)`
dynamic-dispatch detection) are folded into `ast_audit.py`'s `_Visitor`,
which is now the **one** guard. `tests/test_kalshi_meta_inventory_001.py`'s
`TestA2RedTeamGuard` was rewritten to test that one guard; the "original
guard bypasses N of 22" empirical finding is preserved as a frozen,
hand-copied historical reproduction (not a second live guard).

### Newly-visible call sites

Widening the vocabulary made 7 previously-invisible call sites appear (5
named by the launching task, plus 2 a per-line dedup in the prior delta test
had hidden). Each was hand-reviewed against the containment/regular-file
invariant the allowlist exists to enforce and recorded in `ast_audit.
ALLOWLIST` with an inline written justification:

| Site | Verdict | Why |
|---|---|---|
| `segment.py:1338` `SegmentWriter.rotation_due` `.stat()` | SAFE | writer's own segment, already-accepted threat model |
| `segment.py:1566` `SegmentWriter._close_stages` `.stat()` | SAFE | writer's own segment |
| `segment.py:2173` `verify_segment` `.stat()` | **REPORTED FINDING, not independently safe** | see below |
| `segment.py:2348` `_abandoned_residue` `.stat()` | SAFE | downstream of the function's own already-allowlisted containment checks; size-only |
| `evidence_fs.py:266` `open_bounded_gzip` `gzip.GzipFile(fileobj=...)` | SAFE | operates on an in-memory `BytesIO` already produced by `bounded_read`, never touches the filesystem |
| `segment.py:1114` `_quarantine_abandoned_events` `.stat()` | SAFE | same line/try-except as its already-allowlisted `.exists()` |
| `segment.py:1134` `_open_events` `os.fdopen(...)` | SAFE | wraps an fd `os.open()` already opened one line above (writer-owned, `O_NOFOLLOW`) |

**`segment.py:2173` is the one that is not independently safe.** It sits
inside `verify_segment`, at the exact point A3's argument-shape matrix
already proves is unguarded: when `root=None` is passed explicitly, the
whole containment block above it is skipped, and this `.stat()` runs with
zero containment checking on a symlink-to-FIFO events path -- three lines
before the *already-known* `file_sha256` raw `open()` that is the named
defect A3 reproduces as a hang. `.stat()` itself does not block on a FIFO
(metadata-only), so it does not independently hang -- but it is
corroborating evidence of the same missing-containment root cause, not a
separate defect. It is allowlisted as a **known, tracked, unfixed** gap (no
production edit happens anywhere in this milestone), not blanket-allowlisted
to make the suite green -- the allowlist comment says so explicitly, and
`TestA2RedTeamGuard::test_newly_visible_call_sites_are_all_reviewed_in_the_allowlist`
machine-checks that all 5 named sites carry an explicit entry.

## 2. A9 -- the mutation campaign

`tests/meta_mutation/campaign.py` is the re-runnable artifact: a `Mutation`
dataclass catalogue (`MUTATIONS`) plus `apply_mutation`/`restore_mutation`/
`run_catch_command`/`run_campaign`. Re-run it two ways:

```
python3 tests/meta_mutation/campaign.py          # standalone CLI, exit 1 on any hole
python3 -m pytest tests/test_kalshi_meta_mutation_campaign_001.py -q   # as a suite
```

Each mutation: apply an exact, unique string substitution to ONE file under
`tests/` (never `app/`), run its named proof (`pytest` node id(s)), assert
non-zero exit (the target went red), then restore the original bytes
byte-for-byte and re-assert the round trip. Mutations run strictly one at a
time.

**All of that happens inside a disposable copy of the repository, never in
the working tree** (KALSHI-ARCHIVE-REPLAY-INTEGRITY-001). The original
design applied each mutation to the real file and undid it in a `finally`;
an orphaned reviewer process duly left `tests/meta_runtime/aggregate_work.py`
mutated on disk, because no `finally` runs on SIGKILL. `run_campaign` now
copies the repo into a sandbox (`create_sandbox()` / `isolated_repo()`),
mutates and runs `pytest` there, and only ever READS the live tree; the
sandbox is deleted on success and preserved-with-its-path-printed on
failure. `apply_mutation` / `restore_mutation` / `run_catch_command`
therefore all take a required keyword-only `root=`, and `_write` refuses
structurally to write anywhere under `REPO_ROOT`. Two concurrent campaigns
get two sandboxes and cannot interact.

As defence in depth, a `git diff --quiet -- tests/` cleanliness check plus a
sha256 census of `tests/` and `app/` run before and after every campaign and
fail loudly with the offending paths -- which means **the campaign refuses
to start over a dirty `tests/`**, since a modified file there is
indistinguishable from a leftover mutation. The proofs (normal completion,
SIGINT, SIGTERM, SIGKILL, and two concurrent runs) are in
`tests/test_kalshi_meta_mutation_isolation_001.py`. A sandbox deliberately
contains no `.git`: this checkout's `.git` is a pointer file into a shared
git directory, so a copied one would make any stray `git restore` inside the
sandbox hit the real working tree.

| id | what it falsifies | catch |
|---|---|---|
| M1 | drop `read_text` from the guard's detection set | `TestA2RedTeamGuard::test_the_one_guard_discriminates_the_entire_corpus_both_directions` |
| M2 | drop `verify_segment` from A1's `ENTRY_POINTS` | `TestA1CallGraphInventory` |
| M3 | a genuine SIGKILLed hang reported `COMPLETED` instead of `TIMEOUT_FAIL` | `TestPositiveDirectionFiveHangShapes::test_kills_a_known_infinite_sequence` |
| M4 | `GoodEncoderWithAggregateBound`'s declared ceiling never fires | `TestDiscriminationPlantedEncoders::test_good_encoder_with_a_declared_aggregate_bound_satisfies_the_property` |
| M5 | `reconcile()` derives its verdict from `WriterAccounting.disposition_holds()` (true by construction) instead of an independent read | `TestWriterThreadFaultCatchesRealLoss::test_ledger_disagrees_with_the_durable_disposition` |
| M6 | the duplicate-key classifier's own detection hard-wired empty | `TestProductionIsSilentlyLossy` |
| M7 | drop `root=None` from A3's `ROOT_MODES` | `TestA3ArgumentShapeMatrix` |
| M8 | the A3 driver's top-level exception handler swallows silently | `test_driver_reports_typed_failures_not_silence` (new permanent test -- see below) |
| M9 | the strict-xfail test given an unconditional `pytest.xfail(...)` escape hatch | `test_strict_xfail_tests_have_no_unconditional_escape_hatch` (new permanent test) |
| M10 | the async harness's `# FAULT-WINDOW:` marker prefix changed so no marker matches | `TestFourWindowsReproduceDeterministically::test_window_a_diagnostic_drift_never_blocks_close` |

All 10 currently **CAUGHT**. M8 and M9 were **holes** on first pass -- see
below.

### Holes found and closed

- **M8** (typed failure -> silent continue): no existing A3 matrix cell ever
  makes `verify_segment` raise (it returns typed verdicts, not exceptions,
  for every argument shape the matrix explores), so the driver's own
  exception-reporting path is dead code under the current corpus and the
  campaign's first version left this GREEN. Closed by adding
  `test_driver_reports_typed_failures_not_silence` -- a direct unit test of
  `matrix_cell_driver.py` with a deliberately-unknown `api` value, which
  exercises the driver's error path directly rather than hoping a matrix
  cell happens to.
- **M9** (strict-xfail forced-always): `pytest`'s own PASS/FAIL colour
  (`xfailed`) cannot distinguish "the known defect still reproduces" from
  "the test body was neutered before reaching its real assertion" -- both
  report `xfailed` under `strict=True`. Closed by adding
  `test_strict_xfail_tests_have_no_unconditional_escape_hatch`, a
  SOURCE-level (AST) check that no strict-xfail test in the encoder-fidelity
  harness calls `pytest.xfail`/`skip`/`exit` unconditionally before its real
  assertion.
- **M5** required two iterations to design correctly: the first attempt
  (deriving the "independent" side from `WriterAccounting.accepted`) turned
  out to compute the SAME number as the honest independent read for this
  specific fault/timing window (`accepted` is *defined* as
  `written+failed_after_accept+pending+live`, which happens to still track
  this particular loss correctly before `close()`), so it did not
  demonstrate a real hole -- reported here for honesty, not hidden. The
  corrected mutation derives the verdict from `disposition_holds()` alone,
  which genuinely is true by construction and is what actually catches the
  intended defect.

## 3. Verification run (this session)

- `tests/test_kalshi_meta_inventory_001.py` + 5 `meta_runtime` files: 58 ->
  now includes the mutation-campaign package too; meta-suite subtotal 73
  passed.
- `tests/test_kalshi_fs_totality_harness_001.py`: 255 passed, 1 skipped
  (default); 387 passed (`KALSHI_FS_TOTALITY_FULL=1
  KALSHI_FS_TOTALITY_ALLOW_DEV_ZERO=1`) -- both baselines preserved exactly
  after consolidation.
- `tests/test_kalshi_encoder_fidelity_harness_001.py`: 58 passed, 1 skipped,
  1 xfailed -- unchanged.
- `tests/test_kalshi_async_accounting_harness_001.py`: 19 passed, 1 skipped
  -- unchanged.
- 8 `tests/test_kalshi_archive_*.py` / `test_kalshi_canonical_001.py` /
  `test_kalshi_legacy_import_001.py` / `test_kalshi_segment_integrity_001.py`
  gate files: 402 passed, 3 xfailed.
- Full repo suite (`tests/`): 3890 passed, 6 skipped, 4 xfailed.
- `git status` / `git diff --stat -- app/`: empty. `app/` untouched.

Re-run the whole thing with:

```
python3 -m pytest tests/test_kalshi_meta_inventory_001.py tests/test_kalshi_meta_runtime_*.py tests/test_kalshi_meta_mutation_campaign_001.py -q
python3 -m pytest tests/test_kalshi_fs_totality_harness_001.py -q
KALSHI_FS_TOTALITY_FULL=1 KALSHI_FS_TOTALITY_ALLOW_DEV_ZERO=1 python3 -m pytest tests/test_kalshi_fs_totality_harness_001.py -q
python3 -m pytest tests/test_kalshi_encoder_fidelity_harness_001.py tests/test_kalshi_async_accounting_harness_001.py -q
python3 -m pytest tests/ -q
```
