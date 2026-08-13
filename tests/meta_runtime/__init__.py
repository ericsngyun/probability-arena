"""KALSHI-ARCHIVE-VERIFICATION-META-001 -- meta-verification runtime helpers.

This package tests the TESTS: every module here is a reusable harness
primitive for asking "does our verification mechanism itself have blind
spots", never a change to `app/realtime/*`. Nothing here is imported by
`app/`, and nothing here modifies any file under `app/`.

    parent_timeout.py        -- PARENT-supervised subprocess execution with a
                                 real, external, un-defeatable deadline
                                 (A4: never `signal.alarm` inside the code
                                 under test, always a killable child).
    aggregate_work.py         -- adversarial generators + the total-work
                                 property definition for canonicalization
                                 (A5).
    independent_accounting.py -- two independently-sourced observations of
                                 what a segment writer actually durably
                                 disposed of, plus writer-thread-targeted
                                 fault injection (A7).
"""
