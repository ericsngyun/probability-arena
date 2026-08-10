"""KALSHI-ARCHIVE-VERIFICATION-META-001 A9 -- the mutation campaign.

Everything under `tests/meta_inventory/` and `tests/meta_runtime/` (plus,
for A2's consolidation, `tests/harness_filesystem_totality/ast_audit.py`)
claims to DETECT a class of production defect. A verification harness that
has never been shown to fail when it SHOULD is an unfalsified claim, not a
proven one -- the same discipline this whole milestone applies to
production code (A1-A8) applied recursively to the verification layer
itself.

This package mutates the VERIFICATION INFRASTRUCTURE (never `app/`), runs
the narrowest test that infrastructure exists to make pass, and requires it
to FAIL. A mutation that leaves the target test green is a HOLE: the
verification layer would not have caught the equivalent regression in its
own logic, and is reported as such rather than hidden.

Nothing here ever uses `git checkout --`/`git restore` to undo a mutation --
a prior agent lost uncommitted work that way. Every mutation is applied and
restored by reading/writing the exact file bytes in Python, inside a
`try`/`finally`, so a mutation can never survive a crash mid-campaign.
"""
