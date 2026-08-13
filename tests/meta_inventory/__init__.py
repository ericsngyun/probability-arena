"""KALSHI-ARCHIVE-VERIFICATION-META-001 — test the tests.

This package is deliberately quarantined from `tests/harness_filesystem_
totality/` (the AST/matrix guard this milestone exists to attack) and from
`tests/meta_runtime/` (owned by a separate lane). Nothing here imports for
write access to either; where it imports from `tests.harness_filesystem_
totality` at all, it is a read-only reuse of fixture-building helpers, never
a modification.

Nothing under `app/` is ever written by this package. Every finding against
production code discovered here is REPORTED (in inventory artifacts and test
output), never patched.
"""
